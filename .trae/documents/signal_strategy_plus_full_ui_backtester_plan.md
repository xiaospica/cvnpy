# SignalStrategyPlus 完整化实施计划（对齐 SignalStrategy 与 CtaBacktester）

## 目标

满足 3 个新增需求：
1. 回测可视化 Backtester 功能尽可能对齐 `vnpy_ctabacktester`（不做“简化版”）。
2. `SignalStrategyPlusWidget` 不再是空壳，UI 与 `vnpy_signal_strategy` 同级（直接复用/迁移其 Widget）。
3. `vnpy_signal_strategy_plus` 作为“信号执行 App”，LIVE 与 BACKTESTING 均以“轮询数据库信号→下单/回测”为主要驱动方式，功能基线与 `vnpy_signal_strategy` 一致，仅在此基础上增加实盘/回测两种运行时。

同时修复并收敛当前工程中的偏差：
- 用户已删除的桥接文件/架构文档不主动恢复，除非它们是实现上述需求的必要组成。

---

## 一、现状差距与关键结论

### 1) SignalStrategyPlusWidget 差距
当前 `SignalStrategyPlusWidget` 仅展示一行提示文本，缺失：
- 策略类下拉、添加/初始化/启动/停止、策略管理器面板、日志监控等。

结论：应直接以 `vnpy_signal_strategy/ui/widget.py` 为基线复制实现，并把 engine/app 名称与事件类型替换为 plus 版本。

### 2) PlusBacktester 差距
当前 `vnpy_signal_strategy_plus_backtester` 仅提供“最小回测+打开HTML图”能力，缺失 `vnpy_ctabacktester` 的核心能力：
- 下载数据、委托/成交/日度结果窗口、蜡烛图回放、参数编辑器、优化参数空间编辑器、优化结果表格、策略代码编辑、策略重载、回测图表（pyqtgraph）等。

结论：采用“复制 `vnpy_ctabacktester` → 替换依赖为 SignalStrategyPlus”策略，实现功能对齐。

### 3) “信号执行 App”语义差距（关键）
用户明确：
- SignalStrategyPlus 与其 Backtester 并非常规策略（行情驱动），而是通过轮询数据库获取外部策略产生的信号进行实盘交易与回测。

结论：回测引擎需要支持“on_timer 驱动信号拉取/处理”，而不是仅依赖 `on_bar`。
- LIVE：维持 MySQLSignalStrategy 的线程轮询（或由 on_timer 驱动轮询）。
- BACKTESTING：不启动线程，但在回测回放的每个时间步调用策略 `on_timer`，由策略去数据库读取历史信号并执行下单，回测引擎负责撮合与统计。

---

## 二、实现步骤

### 步骤 A：重做 SignalStrategyPlusWidget（直接迁移 SignalStrategyWidget）

文件：`vnpy_signal_strategy_plus/ui/widget.py`

1. 复制 `vnpy_signal_strategy/ui/widget.py` 的完整实现结构：
   - `SignalStrategyWidget`、`SignalStrategyManager`、`DataMonitor`、`LogMonitor`。
2. 替换引用：
   - engine：`SignalEngine` → `SignalEnginePlus`
   - app 名称：`APP_NAME` → plus 的 `APP_NAME`
   - 事件类型：`EVENT_SIGNAL_STRATEGY` → plus 的策略事件常量（统一为 `EVENT_SIGNAL_PLUS_STRATEGY`，并在引擎端保持一致）。
3. 保持 UI 行为与原版一致：策略面板、查找策略、清空日志、策略按钮状态等。

产出：SignalStrategyPlus 的 UI 功能基线与 SignalStrategy 相同。

### 步骤 B：收敛 SignalStrategyPlus 的“信号轮询”语义（LIVE/回测一致）

文件：
- `vnpy_signal_strategy_plus/mysql_signal_strategy.py`
- `vnpy_signal_strategy_plus/backtesting.py`

1. MySQLSignalStrategy：
   - LIVE：保持线程轮询（run_polling）。
   - BACKTESTING：不启动线程，但在 `on_timer` 中增加“单次拉取并处理历史信号”的逻辑（按回测引擎提供的 `engine.datetime` 截止时间过滤）。
   - 将 `on_timer` 同时承担：
     - “重挂队列处理”（AutoResubmitMixin）
     - “回测模式单次信号拉取”（不 sleep、不启动线程）
2. 回测引擎提供兼容层（用于 MySQLSignalStrategy 复用原实现）：
   - 在 `SignalBacktestingEngine` 上提供 `main_engine` 兼容对象（或直接 `self.main_engine=self`），实现最小方法集合：
     - `get_tick(vt_symbol)`：返回当前时间步的最新价格（用 bar close 构造 TickData）。
     - `get_all_positions/get_all_accounts/get_contract`：返回回测账户与持仓的只读视图（满足 MySQLSignalStrategy 的资产计算逻辑）。
   - 回测循环中，每个 bar/tick 时间步调用策略 `on_timer`（用于拉取信号）再调用 `on_bar`（可选），保证“信号策略”在回测中可运行。

产出：MySQLSignalStrategy 及其派生策略可在 PLUS 回测中以“数据库信号”驱动。

### 步骤 C：将 SignalStrategyPlusBacktester 升级为“完整 CtaBacktester 级别功能”

策略：复制 `vnpy_ctabacktester` 的 engine/ui 结构与所有 UI 组件，替换 backtesting 引擎与策略基类。

新增包：`vnpy_signal_strategy_plus_backtester/`

#### C1. Engine 对齐（复制 BacktesterEngine）
文件：`vnpy_signal_strategy_plus_backtester/engine.py`
对齐功能点：
- `init_engine/init_datafeed`（改为使用 `get_datafeed/get_database`）
- 策略类加载：扫描 `vnpy_signal_strategy_plus/strategies` + 工作目录 `strategies`
- 回测运行线程：`start_backtesting/run_backtesting`
- 优化运行线程：`start_optimization/run_optimization`
- 数据下载：`start_downloading/run_downloading`
- 获取结果：result_df/statistics/values
- 获取展示用数据：all trades/orders/daily results/history
- 策略代码编辑与重载：get_strategy_class_file/reload

替换点：
- `CtaTemplate/TargetPosTemplate` → `SignalTemplatePlus`
- `vnpy_ctastrategy.backtesting.BacktestingEngine` → `vnpy_signal_strategy_plus.backtesting.SignalBacktestingEngine`
- 统计与日度结果类型：使用 plus backtesting 的 DailyResult（必要时对齐字段以兼容 UI 表格）。

#### C2. UI 对齐（复制 BacktesterManager 及其所有对话框/监控器）
文件：`vnpy_signal_strategy_plus_backtester/ui/widget.py`
对齐功能点：
- 回测参数区：vt_symbol/interval/start/end/rate/slippage/size/pricetick/capital
- 数据下载按钮
- 回测结果：统计表、pyqtgraph 资金曲线、委托/成交/日度窗口
- K线/成交点位展示（ChartWidget + CandleItem/VolumeItem）
- 参数编辑器（策略参数）
- 优化设置编辑器（参数空间、GA/BF、max_workers、target）
- 优化结果表格与导出
- 策略代码编辑与重载

替换点：
- 引擎与事件常量引用：改为 plus backtester 的常量
- 日度结果展示：若字段不同，补齐 mapping

产出：功能外观与交互尽可能等同于 `vnpy_ctabacktester`。

### 步骤 D：补齐“策略桥接/发现”机制（必要组件）

用户已删除 `vnpy_signal_strategy_plus/strategies/mysql_signal_strategy.py`。
为了保证 UI 与 Backtester 的“策略类扫描”能发现 MySQLSignalStrategy：
- 重新添加一个轻量桥接模块 `vnpy_signal_strategy_plus/strategies/mysql_signal_strategy.py`，仅导入并导出 MySQLSignalStrategy。

### 步骤 E：补齐根目录架构文档（按需恢复）

用户曾删除根目录两份架构文档。
本轮需求未明确要求恢复，但为了支持“完整 backtester”与“plus 语义收敛”的长期维护，建议恢复：
- `ARCHITECTURE_signal_strategy_plus.md`
- `ARCHITECTURE_signal_strategy_plus_backtester.md`
内容升级点：
- 明确“信号轮询”在 LIVE/BACKTESTING 的执行方式
- 明确 Backtester 与回测引擎、Datafeed、Database 的数据流/事件流
- 给出 Mermaid：类图、回测时序图、UI 调度流程图

---

## 三、验证与验收

1. 编译检查：plus、plus_backtester 全量 py_compile
2. 策略发现：Backtester UI 下拉中可看到 MySQLSignalStrategy 及工作目录策略
3. 回测主流程：
   - 下载数据 → 回测 → 统计/图表/成交/委托/日度窗口可打开
4. 优化流程：
   - OptimizationSettingEditor 配置 → BF/GA 运行 → 优化结果可展示
5. 回测驱动信号：
   - BACKTESTING 模式下 MySQLSignalStrategy 不启动线程
   - on_timer 可按回测引擎时间推进拉取历史信号并触发下单

---

## 四、风险与注意事项

- 回测“数据库轮询信号”需要信号表包含时间字段，并支持按时间范围查询；若表结构不足，需要另行扩展。
- Backtester 的“下载数据”依赖当前 `get_datafeed/get_database` 配置；要用 tushare，需要在 vnpy 的 datafeed 配置里启用。
- 为了不引入额外依赖，优先复用 `vnpy_ctabacktester` 现有 UI 与依赖（pyqtgraph/vnpy.chart）。
