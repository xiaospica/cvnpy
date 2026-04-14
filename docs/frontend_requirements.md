# vnpy 多节点 Web 终端 - 前端需求与架构文档

面向对象: 前端工程师 (本次后端实现已完成, 前端独立开发)

配套文档: [API 开发文档](./api.md)

---

## 1. 产品定位与目标

做一个**跨多台云服务器 vnpy 交易进程的统一监控 + 控制台**, 运营者能在一个浏览器页面里:

- 查看所有节点的健康状态、账户资金、持仓、委托、成交
- 管理每个节点上运行的策略 (初始化/启动/停止/编辑参数/删除/新建)
- 接收实时事件推送 (行情、策略状态、日志)
- 支持未来接入多种策略引擎 (`CtaStrategy`、`SignalStrategyPlus` 以及后续新增), 按引擎 capabilities 动态渲染

**单终端**。前端只与聚合层 (`vnpy_aggregator`, 默认 `:9000`) 交互, 不直接访问节点。

---

## 2. 技术选型 (建议, 非强制)

| 项目 | 建议 | 备注 |
|---|---|---|
| 框架 | Vue 3 + TypeScript | React + TS 亦可 |
| 状态管理 | Pinia | React 选 Zustand |
| 路由 | Vue Router | React 选 React Router v6 |
| UI 组件库 | Element Plus / Ant Design Vue | 表格强, 组件齐 |
| 图表 | Apache ECharts | 权益曲线、成交分布 |
| HTTP 客户端 | Axios | 带全局 interceptor 注入 JWT + 401 跳登录 |
| WebSocket | 原生 WebSocket + 自写重连层 | 或 reconnecting-websocket |
| 构建工具 | Vite | 开发体感最好 |
| 代码规范 | ESLint + Prettier + Husky + lint-staged | |
| 包管理 | pnpm | |

前端源码建议独立仓库, 或在本仓库新建 `web_frontend/` 目录; 构建产物后续放 `vnpy_aggregator/static/` 由 FastAPI 托管 (当前后端未接入 StaticFiles, 上线前在聚合层加一行 `app.mount('/', StaticFiles(...))` 即可)。

---

## 3. 软件架构

### 3.1 分层

```
┌──────────────────────────────────────┐
│ views/ (页面组件)                     │
│   Dashboard · Nodes · Strategies ... │
└─────────────▲────────────────────────┘
              │
┌─────────────┴────────────────────────┐
│ store/ (Pinia)                        │
│   auth · nodes · accounts · strategies│
│   orders · trades · logs              │
└─────────────▲────────────────────────┘
              │
┌─────────────┴────────────────────────┐
│ api/                                  │
│   http.ts (Axios 单例 + interceptor)  │
│   ws.ts   (WebSocket 单例 + 派发)     │
│   endpoints/ (按领域分文件, 强类型)   │
└───────────────────────────────────────┘
```

**规则**:

- `views` 不直接 import `api/`, 必须通过 `store` 访问数据。
- `store` 不持有 Axios/WS 细节, 只调用 `api/endpoints/*` 的方法。
- `api/http.ts` 负责: baseURL = `VITE_AGG_BASE_URL`, 自动注入 `Authorization`, 401 时清空 token 并 `router.push('/login')`。
- `api/ws.ts` 负责: 只有登录态时才连接 `/agg/ws?token=...`, 根据 `msg.topic` 派发给 store, 断线后指数退避重连 (最多 5 次, 失败后顶栏显示告警)。

### 3.2 鉴权流

```
[Login.vue] → store.auth.login(u,p)
              → POST /agg/token
              → 保存到 pinia + localStorage (key: agg_token)
              → router.push('/')
[axios interceptor] 每次请求自动加 Authorization: Bearer {token}
[401 response]   清空 token, 跳 /login
[ws.ts]          登录后自动建立 WS; 登出时 close
```

**不要**把 token 存 cookie, 避免 CSRF。必要时启用 HTTPS 加上 `Secure` localStorage 即可。

### 3.3 WS 消息派发

```typescript
// api/ws.ts
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data)
  switch (msg.topic) {
    case 'account':  accountStore.applyPush(msg); break
    case 'position': positionStore.applyPush(msg); break
    case 'order':    orderStore.applyPush(msg); break
    case 'trade':    tradeStore.applyPush(msg); break
    case 'strategy': strategyStore.applyPush(msg); break
    case 'log':      logStore.applyPush(msg); break
    case 'tick':     tickStore.applyPush(msg); break
  }
}
```

每个 store 的 `applyPush` 按 `node_id` (+ `engine`) 做 upsert, 不要全量重算。

### 3.4 路由守卫

- `/login` 免鉴权
- 其它全部需要 `store.auth.isLoggedIn`, 否则跳 `/login`
- `/settings/users` 需要 admin 角色 (本期后端只有一个 admin, 可简化)

---

## 4. 页面与功能清单 (一期必做)

### 4.1 Login `/login`

- 用户名 + 密码表单
- 提交调用 `POST /agg/token`
- 成功跳转 `/`, 失败红色提示

### 4.2 Dashboard `/`

**上半部指标卡**:

- 在线节点数 / 总节点数
- 总账户权益 (balance 求和)
- 活动策略数 (trading=true 的策略数量)
- 今日成交笔数
- 最新 5 条日志

**下半部**: ECharts 分别画

- 各节点账户权益环形图
- 各引擎策略数量柱状图
- 最近 30 分钟事件频率折线图

依赖接口: `GET /agg/nodes`, `GET /agg/accounts`, `GET /agg/strategies`, `WS` 实时刷新。

### 4.3 节点管理 `/nodes`

- 表格: `node_id / base_url / online(灯) / 延迟 / 已启用引擎 / gateway 连通 / 操作`
- 操作: 查看详情 (弹窗显示 `info` + `health` JSON)、删除、重连 (前端仅调 `DELETE` + `POST` 重加)
- 顶部 "新增节点" 按钮: 弹窗填 `node_id/base_url/username/password`

依赖: `GET/POST/DELETE /agg/nodes`。

### 4.4 账户总览 `/accounts`

- 表格列: 节点 / accountid / 币种 / balance / available / frozen / 更新时间
- 支持按 `node_id`、币种过滤
- 总计栏合计权益
- 支持导出 CSV

依赖: `GET /agg/accounts`, WS `account` topic 刷新。

### 4.5 持仓总览 `/positions`

- 按 `node_id + vt_symbol + direction` 分组
- 列: 节点 / 合约 / 方向 / 数量 / 均价 / 浮动盈亏 / 冻结
- 盈亏颜色: 红涨绿跌 (A股约定)
- 支持导出 CSV

### 4.6 委托 & 成交 `/orders`

两个 Tab:

**Tab 1: 活动委托**

- 列: 节点 / vt_orderid / 合约 / 方向 / 价格 / 数量 / 成交数量 / 状态 / 时间 / 操作(撤单)
- 过滤器: 节点 / 状态 / 合约
- 撤单调用 `DELETE /agg/nodes/{node_id}/proxy/order/{vt_orderid}` (需二次确认)

**Tab 2: 今日成交**

- 列: 节点 / vt_tradeid / 合约 / 方向 / 价格 / 数量 / 时间
- 默认按时间倒序

### 4.7 策略控制台 `/strategies` **(核心)**

**布局**: 三列

**左列 (30%)**: 节点 → 引擎 → 实例 的树形导航

```
- node-a (bj-qmt-01)
    - CtaStrategy (1)
        - ma_cross_001
    - SignalStrategyPlus (2)
        - multistrategy-v5.2.1  [Running]
        - live_order_test       [Stopped]
- node-b (sh-ctp-02)
    - CtaStrategy (0)
```

状态徽标: 绿点=trading, 黄点=inited 未 trading, 灰点=未 init。

**中列 (50%)**: 选中节点时给出节点概要; 选中引擎时给出该引擎的策略类列表 + `[+ 新建策略]` 按钮; 选中实例时给出:

- 顶栏: 状态徽标 + `[Init] [Start] [Stop] [Remove] [Edit]` 按钮
  - 按钮启用规则: 查 engine.capabilities; 按策略当前状态屏蔽非法操作 (例如 `Start` 只在 inited=true && trading=false 时可点)
  - 所有写操作二次确认
- 参数表 (Form, 只读默认, `[Edit]` 切换成可编辑态, 保存调用 PATCH)
- 变量表 (只读, WS 推送实时刷新)

**右列 (20%)**: 该策略实例的最近日志 (按 `log` topic + 按策略名过滤)。

**"新建策略"对话框** (动态表单):

1. 用户点 `[+ 新建策略]`, 对话框打开
2. 步骤一: 选 engine → GET `.../engines/{engine}/classes`
3. 步骤二: 选 class_name → GET `.../classes/{class_name}/params` 得到默认参数
4. 步骤三: 根据 **capabilities** 以及这个表判定 `vt_symbol` / `setting` 是否必填:

| engine | vt_symbol | setting 是否必填 |
|---|---|---|
| CtaStrategy | 必填 (如 `600000.SSE`) | 必填 |
| SignalStrategyPlus | 不显示 | 可选 |

5. 提交 POST 实例创建接口

### 4.8 日志 `/logs`

- 流式表格, 按 level/node/keyword 过滤
- 支持暂停自动滚动
- 来源: WS `log` topic + 可选的 `GET /agg/nodes/{id}/proxy/log?limit=100` (后端本期未实现, 前端先用 WS 追加方式)

### 4.9 设置 `/settings`

- 修改密码 (本期后端未暴露修改密码接口, UI 可先挂空按钮或隐藏)
- 深色/浅色主题切换
- 显示后端版本 (从 `/openapi.json` 取 `info.version`)

---

## 5. 非功能需求

- **响应式**: 桌面优先, 最小宽度 1280px; 不要求手机端
- **主题**: 默认浅色, 支持暗色
- **性能**: 所有列表分页或虚拟滚动, 单页 DOM 不超过 5000 元素
- **写操作保护**:
  - 所有写操作 (`start`/`stop`/`remove`/`cancel_order`/`stop-all`) 二次确认
  - `stop-all` 和 `remove` 必须输入节点名或策略名才能确认
  - 所有操作失败后弹 Toast, 文案取自后端 `detail`
- **国际化**: 一期只做简体中文, 预留 i18n 目录 (使用 vue-i18n)
- **可访问性**: 关键操作按钮 aria-label
- **错误展示**: 401 跳登录; 502/503 顶栏红色告警条显示 "聚合层异常"
- **时间显示**: 所有后端时间戳按**本地时区**渲染, 格式 `YYYY-MM-DD HH:mm:ss`
- **WS 健康**:
  - 顶栏常驻一个 WS 状态小圆点 (绿=连通, 红=断线)
  - 断线自动重连, 间隔 1s/2s/5s/10s/30s, 重连上限 5 次, 失败后用户手动点击重连按钮

---

## 6. 接口对照表 (每页使用的后端路径)

| 页面 | REST | WS topic |
|---|---|---|
| Login | `POST /agg/token` | - |
| Dashboard | `GET /agg/nodes` `GET /agg/accounts` `GET /agg/strategies` | account, strategy, log, trade |
| Nodes | `GET/POST/DELETE /agg/nodes` | - |
| Accounts | `GET /agg/accounts` | account |
| Positions | `GET /agg/positions` | position |
| Orders | `GET /agg/orders` `DELETE /agg/nodes/{id}/proxy/order/{vt_orderid}` | order |
| Trades | `GET /agg/trades` | trade |
| Strategies | `GET /agg/strategies` `GET /agg/nodes/{id}/proxy/strategy/engines` `GET .../classes` `GET .../classes/{c}/params` `POST .../instances` `POST .../init` `POST .../start` `POST .../stop` `DELETE .../instances/{n}` `PATCH .../instances/{n}` | strategy, log |
| Logs | 仅 WS | log |

完整路径和请求体见 [api.md](./api.md)。

---

## 7. 交付约定

### 7.1 目录结构

```
web_frontend/
├── src/
│   ├── api/
│   │   ├── http.ts
│   │   ├── ws.ts
│   │   └── endpoints/
│   │       ├── auth.ts
│   │       ├── nodes.ts
│   │       ├── accounts.ts
│   │       ├── strategies.ts
│   │       └── ...
│   ├── store/
│   ├── views/
│   ├── components/
│   ├── router/
│   ├── i18n/
│   └── main.ts
├── .env.development
├── .env.production
├── vite.config.ts
├── package.json
└── tsconfig.json
```

### 7.2 环境变量

```
# .env.development
VITE_AGG_BASE_URL=http://localhost:9000

# .env.production
VITE_AGG_BASE_URL=https://agg.example.com
```

### 7.3 构建

```bash
pnpm install
pnpm dev      # 本地 5173
pnpm build    # 产出 dist/, 可 rsync 到聚合层的 vnpy_aggregator/static/
```

### 7.4 Mock

本期建议先写 mock 层 (msw 或 axios-mock-adapter) 跑通所有页面, 再对接真实聚合层。mock 数据按 `api.md` 示例即可。

### 7.5 Definition of Done

- [ ] 9 个页面全部可点通, 登录 → 退出闭环
- [ ] WS 断线后顶栏告警并可手动重连
- [ ] 所有写操作经过二次确认, 成功/失败均有 Toast
- [ ] 新建策略对话框能根据 `capabilities` 和引擎正确渲染表单
- [ ] CtaStrategy 引擎新建时 `vt_symbol` 必填校验生效
- [ ] 暗色主题可切换
- [ ] ESLint/Prettier 零告警, `tsc --noEmit` 零错误
- [ ] 至少覆盖 `store` 关键方法的单元测试

---

## 8. 后续扩展 (Phase 2 以后, 前端不在本期承诺)

- 权益曲线历史 (后端接入时序库后)
- 告警规则配置 (账户回撤/策略异常)
- 多用户 + RBAC
- 移动端适配
- 节点分组与标签
