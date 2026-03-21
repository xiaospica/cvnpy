# SignalStrategyPlus 后续需求实施计划

本计划覆盖 4 个需求：
1. SignalStrategyPlus 回测结果补齐：统计/图表/参数优化（对齐 vnpy_ctastrategy/backtesting.py 的结构与能力）
2. 新增一个类似 vnpy_ctabacktester 的回测可视化 App（对应 vnpy_signal_strategy_plus）
3. 为 vnpy_signal_strategy_plus 以及“回测可视化 App”在**项目根目录**新增架构说明文档（详尽、可维护、面向架构师）
4. 确保 vnpy_signal_strategy_plus 的实盘与回测**兼容** vnpy_signal_strategy_plus/MySQLSignalStrategy 及其派生策略

---

## 一、现状评估（基于当前代码）

### 1) 兼容性问题（必须先解决）

当前 `vnpy_signal_strategy_plus/mysql_signal_strategy.py` 的 `MySQLSignalStrategy` 继承自 `vnpy_signal_strategy.template.SignalTemplate`，而 SignalStrategyPlus 的策略加载器与引擎是围绕 `SignalTemplatePlus` 设计的：
- 引擎加载策略类只扫描 `vnpy_signal_strategy_plus/strategies/` 目录，并要求策略类继承 `SignalTemplatePlus`（当前引擎实现如此）。
- `MySQLSignalStrategy` 不在 `strategies/` 目录，且继承的基类不是 `SignalTemplatePlus`。

因此，若不调整：
- SignalStrategyPlus 实盘引擎无法自动加载 MySQLSignalStrategy；
- SignalStrategyPlus 回测引擎也无法按统一模板对其进行回放（虽然“鸭子类型”可能能跑，但会形成不可控分叉）。

结论：需要先完成“策略基类统一 + 策略加载路径统一”的收敛改造，再做回测统计/图表/优化与 UI App。

---

## 二、需求 1：SignalStrategyPlus 回测统计/图表/参数优化（对齐 CTA）

目标：把 `vnpy_signal_strategy_plus/backtesting.py` 从“最小撮合”升级为可用于研究与对比的回测组件，参考 `vnpy_ctastrategy/backtesting.py` 的结构与 API 设计。

### 2.1 需要对齐的能力清单（分层实现）

**A. 回测输入参数（与 CTA 对齐）**
- vt_symbol、interval、start/end、rate、slippage、pricetick、capital
- backtesting mode：先实现 BAR（后续扩展 TICK）
- 输出日志接口：`output`（与 CTA 一样可被 backtester 引擎重定向到 UI 日志）

**B. 回测核心数据（与 CTA 对齐）**
- orders/trades 的记录与可回放
- 持仓/现金/冻结（至少现金与持仓，冻结先简化）
- 日度结果 `DailyResult`：用于绘制资金曲线/收益曲线/回撤曲线

**C. 统计指标（复用 CTA 依赖）**
复用 vnpy_ctastrategy/backtesting.py 已存在依赖（pandas/numpy/plotly/empyrical）：
- total_return、annual_return、max_drawdown、sharpe、win_rate、profit_loss_ratio 等

**D. 图表输出（Plotly）**
- 资金曲线（equity curve）
- 回撤曲线
- 日收益直方图
- 交易分布/成交统计（先简版）

**E. 参数优化入口**
- 参考 `vnpy.trader.optimize.OptimizationSetting`、BF/GA
- 以“回测函数可重复调用 + 返回统计指标”作为优化目标
- 输出结果表（DataFrame）与最优参数组合

### 2.2 回测撮合规则（谨慎范围）

SignalStrategy 的回测与 CTA 不同：多数策略是“信号→下单”，撮合规则可先采用 CTA 的简化版：
- 限价单：bar close 成交（或 next bar open 成交），先实现一种并文档明确
- 市价单：用 bar close 成交
- 手续费：按成交额 * rate
- 滑点：按方向加减固定 slippage，再 round_to(pricetick)

并在架构文档中明确“研究假设”，避免与真实交易误对齐。

---

## 三、需求 2：新增回测可视化 App（类似 vnpy_ctabacktester）

新增包建议命名：`vnpy_signal_strategy_plus_backtester/`

### 3.1 结构参考（对齐 vnpy_ctabacktester）

- `engine.py`
  - `BacktesterEnginePlus(BaseEngine)`：负责
    - 初始化 datafeed（复用 `get_datafeed()`，配置为 tushare 时自动走 vnpy_tushare）
    - 加载 SignalStrategyPlus 策略类（扫描 vnpy_signal_strategy_plus/strategies 与项目根 strategies）
    - 运行回测/优化（后台线程），完成后推送事件给 UI
  - 事件常量：
    - `EVENT_SIGNAL_PLUS_BACKTESTER_LOG`
    - `EVENT_SIGNAL_PLUS_BACKTESTING_FINISHED`
    - `EVENT_SIGNAL_PLUS_OPTIMIZATION_FINISHED`

- `ui/widget.py`
  - 参考 ctabacktester 的 UI：
    - 策略类选择、vt_symbol、周期、时间范围、费率滑点、初始资金
    - 参数编辑区（从 strategy.parameters 读取）
    - 运行按钮：回测/优化
    - 结果展示：统计表 + Plotly 图表（或 HTML/图片方式嵌入）

- `__init__.py`
  - `SignalStrategyPlusBacktesterApp(BaseApp)` 提供主界面入口

### 3.2 交互边界（保持简洁但可用）

第一阶段只做：
- 回测：统计 + 资金曲线图
- 优化：BF（穷举）优先，GA 可后置

---

## 四、需求 3：根目录架构文档（面向架构师）

在项目根目录新增两份文档（避免与现有 architecture.md 混淆）：
- `ARCHITECTURE_signal_strategy_plus.md`
- `ARCHITECTURE_signal_strategy_plus_backtester.md`

文档内容要求（“软件架构师级别”）：
- 组件边界、职责、依赖关系
- LIVE/BACKTESTING 两套运行时的差异与数据流
- 关键对象模型（strategy、engine、backtesting engine、datafeed、main engine）
- 关键流程图与时序图（Mermaid）
- 风险与约束（回测假设、数据源一致性、实盘安全开关）

---

## 五、需求 4：兼容 vnpy_signal_strategy_plus/MySQLSignalStrategy 派生策略

### 5.1 兼容目标
- **实盘**：SignalEnginePlus 能加载并运行 MySQLSignalStrategy（及其派生类），策略能正常收订单/成交回报，能下单/撤单。
- **回测**：SignalBacktestingEngine 能运行 MySQLSignalStrategy 的“派生策略”（通常派生策略会实现 on_bar/on_tick 信号逻辑，而不是依赖数据库轮询线程）。

### 5.2 具体改造方案（收敛到一套基类）

1. 将 `vnpy_signal_strategy_plus/mysql_signal_strategy.py` 改为继承 `SignalTemplatePlus`（而非旧的 `SignalTemplate`），确保统一接口。
2. 在 `vnpy_signal_strategy_plus/strategies/` 增加轻量导入模块（例如 `strategies/mysql_signal_strategy.py`）以便策略加载器自动发现类。
3. SignalEnginePlus 的策略加载器明确只接受继承 `SignalTemplatePlus` 的类，避免“混合基类”导致行为不可预测。
4. 回测引擎（SignalBacktestingEngine）只依赖 SignalTemplatePlus 的 send_order/cancel_order/write_log 接口，使派生策略可复用。

（可选增强）如果你希望兼容旧 SignalTemplate 策略也能在 Plus 中运行，则在 SignalEnginePlus 增加“鸭子类型”检测与适配器层；但第一阶段建议先收敛，减少维护成本。

---

## 六、实施顺序（推荐）

1. 先做 **兼容性收敛**（需求 4），确保 MySQLSignalStrategy 能被 Plus 实盘/回测加载。
2. 再补齐 **回测统计/图表/优化**（需求 1），为 UI 提供稳定 API。
3. 开发 **Backtester App**（需求 2），复用回测引擎输出。
4. 最后写两份 **根目录架构文档**（需求 3），并用 Mermaid 图把最终结构固定下来。

---

## 七、验证清单

1. 语法编译：
   - `vnpy_qmt`、`vnpy_signal_strategy_plus`、`vnpy_signal_strategy_plus_backtester`
2. 回测最小用例：
   - 使用 tushare datafeed 拉取 1 个标的的 bar 数据并跑通统计与图表输出
3. UI 回测：
   - Backtester App 可选择策略、参数、运行并展示结果
4. 实盘加载：
   - SignalStrategyPlusApp 能加载 MySQLSignalStrategy 派生策略，事件路由与下单链路不报错

