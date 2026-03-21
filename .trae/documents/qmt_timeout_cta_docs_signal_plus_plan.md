# 新需求实施计划：QMT超时撤单、CTA文档完善、SignalStrategyPlus（实盘+回测）

## 总览

本计划覆盖三个交付物：
1. **vnpy_qmt（实盘网关）** 增加“超时自动撤单”能力（谨慎、可配置、默认关闭/保守）。
2. **vnpy_ctastrategy** 输出一份“非常详细”的架构文档（含 UML/流程图/时序图，Markdown + Mermaid）。
3. 参考 CTA 架构，设计并落地一个 **vnpy_signal_strategy_plus**：同时支持实盘与回测；实盘对接 `vnpy_qmt`，回测通过 `vnpy_tushare` 获取历史数据。

---

## 1) vnpy_qmt 增加超时撤单（实盘网关）

### 1.1 现状梳理（基于当前代码）
- `QmtGateway` 已经注册了 `EVENT_TIMER` 并周期执行：`query_trade/query_order/query_account/query_position`。
- `TD` 维护 `self.orders: Dict[str, OrderData]`，订单主键为 `order_remark`（即 vnpy 的 `orderid`）。
- **关键风险**：
  - `TD.cancel_order()` 当前是 `cancel_order_stock_async(... order_id=order.reference)`，但 `send_order` 创建的 `OrderData` 初始 **没有 reference**（reference 会在后续 `on_stock_order` 回调中才填充）。因此“超时撤单”必须处理“reference 尚未可用”的边界。

### 1.2 设计原则（实盘需要仔细推敲）
- **单一职责**：网关只负责“发现超时 + 发起撤单请求”，不做策略重挂。
- **可配置/默认保守**：
  - 增加 setting：`订单超时秒数`、`超时撤单检查周期秒`、`是否启用超时撤单`。
  - 默认 `是否启用超时撤单=False`（避免真实账户误撤单）。
- **只处理活动单**：仅对 `SUBMITTING/NOTTRADED/PARTTRADED` 且 `traded < volume` 的订单发起撤单。
- **幂等**：同一订单只发一次撤单（需要 `cancel_sent` 标记）。
- **reference 不可用时不撤**：
  - 若 `order.reference` 为空，说明柜台订单号未回填，不应盲撤；等下一轮 `query_order`/回报更新后再处理。

### 1.3 代码改造点

#### A. QmtGateway：增加超时撤单配置与定时检查
文件：`vnpy_qmt/qmt_gateway.py`
- `default_setting` 新增：
  - `是否启用超时撤单: bool`
  - `订单超时秒数: int`
  - `超时撤单检查周期秒: int`
- `__init__` 增加计数器/节流字段。
- `process_timer_event` 现有查询逻辑保留，在合适节流点调用：
  - `self.check_order_timeout()`
- 新增 `check_order_timeout()`（必须增加 docstring）：
  - 遍历 `self.td.orders` 找出超时活动单
  - 对满足条件且 `reference` 已具备的订单，调用 `self.cancel_order(order.create_cancel_request())` 或直接 `self.td.cancel_order(order.orderid)`
  - 记录撤单已发出标记（放在 TD 或 gateway 内部 dict）

#### B. TD：补齐“可撤单所需字段”的回填时机
文件：`vnpy_qmt/td.py`
- `send_order` 创建 OrderData 时写入 `datetime=datetime.datetime.now()`（用于超时计时）。
- 在 `on_order_stock_async_response`（下单异步响应）中：
  - 若成功（`response.order_id` 有值，且 `error_msg` 为空），将 `old_order.reference = response.order_id` 及时回填。
- 在 `on_stock_order`（订单查询/推送）中：
  - 已有 `reference=order.order_id`，继续保持。
  - 对 `REJECTED/CANCELLED/ALLTRADED` 的终态订单，从“超时跟踪字典/撤单标记”中清理。
- 为新增/改造函数全部补充 docstring。

### 1.4 验证方案
- 只做“逻辑验证 + 非真实账户回归”，不在计划阶段承诺真实下单。
- 增加一段可复现的“超时撤单模拟路径”：
  - 通过将 `订单超时秒数` 调小，在测试环境中观察 `check_order_timeout` 触发的撤单请求与日志。

---

## 2) vnpy_ctastrategy 架构与功能详细文档（含 UML/流程/时序）

### 2.1 文档输出位置
- 输出 Markdown 文件到：`vnpy_ctastrategy/ARCHITECTURE.md`
- 图使用 Mermaid（方便版本控制与渲染）。

### 2.2 文档大纲（非常详细）

1. **模块概览**
   - `base.py`：常量、枚举（EngineType/BacktestingMode）、StopOrder 数据结构与事件类型
   - `engine.py`：CtaEngine（实盘引擎）
   - `template.py`：CtaTemplate/TargetPosTemplate 等策略基类
   - `backtesting.py`：BacktestingEngine（回测/优化）

2. **软件架构（分层）**
   - VeighNa Trader 基础：EventEngine/MainEngine/BaseGateway/BaseEngine
   - CTA App：CtaStrategyApp → CtaEngine
   - 外部依赖：database/datafeed/optimize

3. **关键数据结构**
   - 订单/成交/持仓/账户对象
   - StopOrder（本地 stop 与服务器 stop 的差异）
   - Map 关系：`symbol_strategy_map`、`orderid_strategy_map`、`strategy_orderid_map`

4. **实盘工作流（事件驱动）**
   - Tick → CtaEngine.process_tick_event → strategy.on_tick
   - Order → process_order_event → strategy.on_order / strategy.on_stop_order
   - Trade → process_trade_event → 更新 pos → strategy.on_trade → sync data

5. **回测工作流**
   - BacktestingEngine.set_parameters/load_data/run_backtesting
   - Bar/Tick 回放驱动 → 撮合（limit/stop）→ 生成 TradeData/OrderData
   - 统计：daily result、绩效指标、图表

6. **优化工作流**
   - OptimizationSetting → BF/GA
   - 并行执行与结果汇总

7. **UML/流程/时序图（Mermaid）**
   - 类图：CtaEngine、CtaTemplate、BacktestingEngine、StopOrder
   - 时序图（实盘）：策略下单→主引擎→网关→回报→回调
   - 时序图（回测）：回放→撮合→订单/成交→策略回调
   - 流程图：引擎 init/start/stop 生命周期

### 2.3 交付验收标准
- 文档可直接阅读并可作为新成员入门材料。
- Mermaid 图可被渲染（语法正确）。
- 每个核心模块都有“职责、主要类、主要函数、关键流程”。

---

## 3) 设计并实现 vnpy_signal_strategy_plus（实盘 + 回测）

### 3.1 目标与边界
- 目标：参考 CTA 的“双引擎形态”，让 SignalStrategy 同时支持：
  - **实盘（LIVE）**：沿用事件引擎驱动，订单通过 `MainEngine.send_order` 下发到 `vnpy_qmt`。
  - **回测（BACKTESTING）**：使用 `vnpy_tushare` 拉取数据（通过 VeighNa 的 Datafeed 机制），在本地回放并模拟撮合/订单回报。
- 边界：
  - 回测先支持“Bar 级别”即可；撮合模型先做最小可用（limit 成交、手续费/滑点可配置）。

### 3.2 推荐架构（对齐 CTA）
新增包：`vnpy_signal_strategy_plus/`
- `base.py`
  - `APP_NAME`、`EngineType(LIVE/BACKTESTING)`、事件常量
- `template.py`
  - `SignalTemplatePlus`：接口对齐现有 `SignalTemplate`，但补齐回测所需 hook
- `engine.py`
  - `SignalEnginePlus(BaseEngine)`：类似 `CtaEngine` 的 LIVE 引擎
  - 管理：策略加载、symbol 映射、orderid 映射、事件注册
- `backtesting.py`
  - `SignalBacktestingEngine`：
    - 参数：vt_symbol、interval、start/end、手续费、滑点、初始资金
    - 数据加载：通过 `vnpy_tushare` Datafeed (`HistoryRequest`)
    - 回放：驱动策略 `on_bar/on_tick`，并实现最小撮合
- `app.py/__init__.py`
  - 提供 `SignalStrategyPlusApp(BaseApp)` 供主界面加载
- `ui/`（可选）
  - 复用现有 SignalStrategyWidget 或后续再做

### 3.3 Live（对接 vnpy_qmt）的实现要点
- `SignalEnginePlus` 复用现有 SignalEngine 的订单映射逻辑：
  - 下单后把 `vt_orderid → strategy` 建立映射
  - 在 `EVENT_ORDER/EVENT_TRADE` 回报时路由给对应策略
- 策略层重挂/撤单逻辑可复用现有 `AutoResubmitMixin`（保持策略层职责）。

### 3.4 Backtesting（对接 vnpy_tushare）的实现要点
- 数据加载：
  - 使用 `get_datafeed()` 获取当前配置的数据源（可切换到 tushare）
  - 通过 `HistoryRequest` 获取 bar 数据
- 撮合与回报：
  - 生成 OrderData/TradeData，并回调策略 `on_order/on_trade`
  - 维护账户与持仓（最小模型：现金、冻结、持仓数量、成交价）
- 输出结果：
  - PnL、胜率、回撤、订单统计等（可先做简版，后续对齐 CTA）

### 3.5 渐进式交付里程碑
1. **MVP-1（框架）**：SignalStrategyPlus 引擎骨架 + EngineType + 策略加载/事件注册
2. **MVP-2（回测）**：接入 tushare 数据加载 + bar 回放 + 最小撮合
3. **MVP-3（实盘）**：对接 vnpy_qmt 下单/撤单 + 回报路由
4. **MVP-4（UI/报告）**：补充回测报告/参数优化入口

---

## 4) 统一代码规范要求
- 新增函数必须添加简洁 docstring（说明用途、输入、边界）。
- 实盘相关逻辑默认保守并可配置开关。

---

## 5) 本计划交付物清单
- 代码：
  - `vnpy_qmt/qmt_gateway.py`、`vnpy_qmt/td.py`（超时撤单）
  - 新增 `vnpy_signal_strategy_plus/`（实盘+回测）
- 文档：
  - `vnpy_ctastrategy/ARCHITECTURE.md`

