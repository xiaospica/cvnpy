# vnpy Web 监控/控制终端 API 文档

版本: v1 (2026-04)

本文档描述两层 API:

- **节点层** (Node Layer): `vnpy_webtrader` 内置的 FastAPI, 部署在每台跑交易进程的机器上, URL 前缀 `/api/v1`。
- **聚合层** (Aggregator Layer): `vnpy_aggregator` 独立部署, 统一面向前端, URL 前缀 `/agg`。

前端原则上只调用聚合层; 节点层路由供聚合层内部/故障应急使用, 也会列在本文档中以便直接调试单节点。

---

## 0. 通用约定

### 0.1 认证

两层都使用 **OAuth2 Password + JWT Bearer**, 流程:

1. `POST /agg/token` (前端) 或 `POST /api/v1/token` (节点层调试) 获取 access token。
2. 后续请求头加入 `Authorization: Bearer <token>`。
3. WebSocket 使用 query string: `ws://.../ws?token=<token>`。
4. JWT 默认 30 分钟过期 (节点层) / 60 分钟过期 (聚合层), 过期后 401, 客户端需要重新登录。

### 0.2 统一错误响应

FastAPI 默认错误格式:

```json
{"detail": "..."}
```

策略写操作的成功响应统一为:

```json
{"ok": true, "message": "started", "data": null}
```

失败时由后端转成 HTTP 错误码 (通过 `deps.unwrap_result` 判断 `data.http_status`):

| HTTP 状态 | 含义 |
|---|---|
| 401 | 未登录 / token 失效 |
| 404 | 资源不存在 (引擎/策略/节点/合约) |
| 409 | 状态冲突 (如 stop 一个未运行的策略) |
| 501 | 引擎不支持该操作 (如 `edit_strategy` on 不支持 edit 的引擎) |
| 502 | 聚合层转发节点失败 (节点离线或网络错误) |
| 503 | 服务未就绪 (RPC client 未连接) |

### 0.3 术语

- **node**: 一台跑 vnpy 交易进程 + vnpy_webtrader 的机器, 用 `node_id` 唯一标识。
- **engine / app_name**: 交易进程里的策略引擎 App, 例如 `CtaStrategy`、`SignalStrategyPlus`。
- **strategy instance / name**: 某个 engine 内运行的策略实例, 由 `strategy_name` 唯一标识。
- **class_name**: 策略类的 Python 类名, 对应源代码文件里的一个 `class`。

---

## 1. 节点层 `vnpy_webtrader` (`/api/v1`)

### 1.1 登录

#### `POST /api/v1/token`

Body (form-urlencoded):

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| username | string | 是 | 节点配置里的用户名 |
| password | string | 是 | 节点配置里的密码 |

响应:

```json
{"access_token": "eyJ...", "token_type": "bearer"}
```

---

### 1.2 节点自描述

#### `GET /api/v1/node/info`

返回节点身份与引擎清单, 供聚合层识别。

```json
{
  "node_id": "bj-qmt-01",
  "display_name": "北京QMT节点",
  "started_at": 1715000000.0,
  "uptime": 12345.6,
  "gateways": [
    {"name": "QMT_SIM", "connected": true}
  ],
  "engines": ["CtaStrategy", "OmsEngine", "SignalStrategyPlus"],
  "strategy_engines": [
    {
      "app_name": "SignalStrategyPlus",
      "display_name": "Signal策略Plus",
      "event_type": "EVENT_SIGNAL_STRATEGY_PLUS",
      "capabilities": ["add", "edit", "init", "remove", "start", "stop"]
    }
  ]
}
```

#### `GET /api/v1/node/health`

```json
{
  "status": "ok",
  "uptime": 12345.6,
  "event_queue_size": 3,
  "gateway_status": {"QMT_SIM": true}
}
```

---

### 1.3 交易信息只读

| 路径 | 方法 | 说明 |
|---|---|---|
| `/api/v1/account` | GET | 账户资金列表 |
| `/api/v1/position` | GET | 当前持仓列表 |
| `/api/v1/order` | GET | 当前委托列表 |
| `/api/v1/trade` | GET | 今日成交列表 |
| `/api/v1/tick` | GET | 已订阅行情快照 |
| `/api/v1/contract` | GET | 所有合约 |

返回都是数组, 元素为 vnpy `AccountData`/`PositionData` 等的 dict 序列化, 例如:

```json
[
  {
    "accountid": "123456",
    "balance": 1000000.0,
    "frozen": 0.0,
    "vt_accountid": "QMT_SIM.123456",
    "gateway_name": "QMT_SIM"
  }
]
```

### 1.4 交易写操作

#### `POST /api/v1/order`

请求体 (JSON):

```json
{
  "symbol": "600000",
  "exchange": "SSE",
  "direction": "多",
  "type": "限价",
  "volume": 100,
  "price": 10.5,
  "offset": "开",
  "reference": ""
}
```

返回 `vt_orderid: string`。

#### `DELETE /api/v1/order/{vt_orderid}`

撤单, 无响应体 (204)。

#### `POST /api/v1/tick/{vt_symbol}`

订阅行情, 例 `/api/v1/tick/600000.SSE`。

---

### 1.5 策略管理 (核心)

所有路径都走 `StrategyEngineAdapter`, 写操作返回 `OpResult` 结构。

#### `GET /api/v1/strategy/engines`

返回节点已加载的策略引擎列表, 用于前端动态渲染引擎下拉框。

```json
[
  {
    "app_name": "SignalStrategyPlus",
    "display_name": "Signal策略Plus",
    "event_type": "EVENT_SIGNAL_STRATEGY_PLUS",
    "capabilities": ["add","edit","init","remove","start","stop"]
  }
]
```

#### `GET /api/v1/strategy/engines/{engine}/classes`

返回该引擎可用的策略类名列表:

```json
["MultiStrategySignalStrategyPlus", "LiveOrderTestStrategy"]
```

#### `GET /api/v1/strategy/engines/{engine}/classes/{class_name}/params`

返回该策略类的默认参数:

```json
{"db_host": "localhost", "poll_interval": 1.0}
```

#### `GET /api/v1/strategy`

跨所有引擎列出策略实例 (`StrategyInfo[]`)。

#### `GET /api/v1/strategy/engines/{engine}`

某引擎的实例列表。

#### `GET /api/v1/strategy/engines/{engine}/instances/{name}`

单个实例的详细快照:

```json
{
  "engine": "SignalStrategyPlus",
  "name": "multistrategy-v5.2.1",
  "class_name": "MultiStrategySignalStrategyPlus",
  "vt_symbol": null,
  "author": "",
  "inited": true,
  "trading": true,
  "parameters": {"db_host": "localhost", "poll_interval": 1.0},
  "variables": {"last_signal_id": 42}
}
```

#### `POST /api/v1/strategy/engines/{engine}/instances`

新建策略实例。Body:

```json
{
  "class_name": "MultiStrategySignalStrategyPlus",
  "strategy_name": "multistrategy-v5.2.1",
  "vt_symbol": null,
  "setting": {}
}
```

**字段按引擎差异**:

| 字段 | CtaStrategy | SignalStrategyPlus | LegacySignalStrategy |
|---|---|---|---|
| `class_name` | 必填 | 必填 | 必填 |
| `strategy_name` | 必填 (任意) | 必填, 需与策略类里硬编码的 `strategy_name` 一致 | 同 Plus |
| `vt_symbol` | **必填** (如 `600000.SSE`) | 可选 (通常忽略) | 可选 |
| `setting` | **必填**, 写入 setting 文件 | 可选, 通过 `update_setting` 应用 | 可选 |

响应:

```json
{"ok": true, "message": "added", "data": null}
```

#### `POST /api/v1/strategy/engines/{engine}/instances/{name}/init`

同步返回 `{ok, message}`, 内部会对 Future 等待最多 30 秒。

#### `POST /api/v1/strategy/engines/{engine}/instances/{name}/start`

#### `POST /api/v1/strategy/engines/{engine}/instances/{name}/stop`

#### `DELETE /api/v1/strategy/engines/{engine}/instances/{name}`

#### `PATCH /api/v1/strategy/engines/{engine}/instances/{name}`

Body: `{"setting": {"poll_interval": 2.0}}`。引擎 `capabilities` 里没有 `edit` 时返回 501。

#### 批量操作

- `POST /api/v1/strategy/engines/{engine}/actions/init-all`
- `POST /api/v1/strategy/engines/{engine}/actions/start-all`
- `POST /api/v1/strategy/engines/{engine}/actions/stop-all`

---

### 1.6 WebSocket

#### `WS /api/v1/ws?token=<jwt>`

消息格式 (所有推送都遵循此结构):

```json
{
  "topic": "strategy",
  "engine": "SignalStrategyPlus",
  "node_id": "bj-qmt-01",
  "ts": 1715000000.0,
  "data": { "strategy_name": "multistrategy-v5.2.1", "inited": true, "trading": true }
}
```

**topic 枚举**:

| topic | engine 字段 | 触发条件 | data 结构 |
|---|---|---|---|
| `tick` | 空 | 有行情推送 | TickData dict |
| `order` | 空 | 委托状态变更 | OrderData dict |
| `trade` | 空 | 成交回报 | TradeData dict |
| `position` | 空 | 持仓变化 | PositionData dict |
| `account` | 空 | 账户资金变化 | AccountData dict |
| `log` | 空 | 任意日志事件 | `{msg, gateway_name, level, time}` |
| `strategy` | 引擎 app_name | 策略状态/变量变化 | 引擎各自的 strategy data dict |

---

## 2. 聚合层 `vnpy_aggregator` (`/agg`)

### 2.1 登录

#### `POST /agg/token`

表单: `username` / `password`, 使用聚合层独立的管理员账号 (与节点层凭据解耦)。

---

### 2.2 节点注册表

#### `GET /agg/nodes`

```json
[
  {
    "node_id": "bj-qmt-01",
    "base_url": "https://node1.example.com",
    "online": true,
    "last_heartbeat": 1715000000.0,
    "info": { "...": "节点 /api/v1/node/info 最新返回" },
    "health": { "...": "节点 /api/v1/node/health 最新返回" }
  }
]
```

#### `POST /agg/nodes`

Body:

```json
{
  "node_id": "sh-ctp-02",
  "base_url": "https://node2.example.com",
  "username": "vnpy",
  "password": "vnpy",
  "verify_tls": true
}
```

#### `DELETE /agg/nodes/{node_id}`

---

### 2.3 跨节点扇出 (只读)

所有这类接口返回统一结构 `FanoutItem[]`:

```json
[
  {"node_id": "bj-qmt-01", "ok": true, "data": [/*节点返回原样*/], "error": null},
  {"node_id": "sh-ctp-02", "ok": false, "data": null, "error": "offline"}
]
```

| 路径 | 对应节点路径 |
|---|---|
| `GET /agg/accounts` | `/api/v1/account` |
| `GET /agg/positions` | `/api/v1/position` |
| `GET /agg/orders` | `/api/v1/order` |
| `GET /agg/trades` | `/api/v1/trade` |
| `GET /agg/strategies` | `/api/v1/strategy` |

---

### 2.4 透传写接口

#### `{GET|POST|DELETE|PATCH} /agg/nodes/{node_id}/proxy/{path:path}`

把请求透传到节点的 `/api/v1/{path}`。前端构造子路径:

示例: 前端要启动 `node-a` 上 `SignalStrategyPlus` 引擎的 `multistrategy-v5.2.1` 策略:

```
POST /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/instances/multistrategy-v5.2.1/start
```

响应: 节点返回的原始 JSON (成功) 或 HTTP 错误。

---

### 2.5 WebSocket 汇流

#### `WS /agg/ws?token=<jwt>`

单条 WS 通道, 聚合层内部为每个节点维持一条上游 WS, 收到的消息补上 `node_id` 后转发。

消息结构与节点层一致, 但 `node_id` 一定会存在:

```json
{
  "topic": "strategy",
  "engine": "SignalStrategyPlus",
  "node_id": "bj-qmt-01",
  "ts": 1715000000.0,
  "data": { "...": "..." }
}
```

断线重连由客户端负责。聚合层每 10 秒心跳, 连续 3 次失败会把节点标记 offline 并停止对该节点的 WS。

---

## 3. 典型调用流程

### 3.1 前端登录并拉取账户

```
POST /agg/token                      -> {access_token}
GET  /agg/nodes                      -> 列出节点, 渲染 dashboard
GET  /agg/accounts                   -> 跨节点账户总览
WS   /agg/ws?token=...               -> 订阅实时事件
```

### 3.2 启动某节点的策略

```
GET  /agg/nodes/node-a/proxy/strategy/engines                    -> 列引擎
GET  /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/classes
GET  /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/classes/MultiStrategySignalStrategyPlus/params
POST /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/instances
     {class_name, strategy_name, setting}
POST /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/instances/multistrategy-v5.2.1/init
POST /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/instances/multistrategy-v5.2.1/start
```

之后 WS 会推送 `{topic:"strategy", engine:"SignalStrategyPlus", node_id:"node-a", data:{...inited:true, trading:true}}`。

### 3.3 批量停止

```
POST /agg/nodes/node-a/proxy/strategy/engines/SignalStrategyPlus/actions/stop-all
```

---

## 4. 直接 Swagger

两层都启用了 FastAPI 自动 OpenAPI:

- 节点层: `http://<node>:8000/docs`
- 聚合层: `http://<agg>:9000/docs`

本文档是可读指南 + 字段对照; 字段级 schema 直接看 `/docs` 更准确。
