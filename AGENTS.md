# AGENTS.md - A股实盘交易开发指南

本指南旨在辅助 AI Agent 在 `vnpy_strategy_dev` 工程中进行 A 股实盘交易模块的二次开发。

## 1. 工程概况

- **框架**: VeighNa (vn.py) 定制版
- **语言**: Python 3.10+
- **核心接口**: QMT (vnpy\_qmt)
- **解释器**：F:\Program\_Home\vnpy\python.exe
- **主要路径**:
  - `vnpy/`: 核心框架源码
  - `vnpy_qmt/`: QMT 柜台接口
  - `vnpy/trader/`: 交易业务逻辑与数据结构
  - `vnpy/event/`: 事件驱动引擎
  - `docs/architecture.md`: 本工程架构文档，涉及跨模块持久化、webtrader/mlearnweb 对接、策略权益 journal 等关键设计时必须同步更新

## 2. 编码规范 (Python)

- **风格**: 遵循 PEP 8。
- **类型提示**: **必须**使用 Type Hints (e.g., `def func(a: int) -> str:`)。
- **文档字符串**: 使用 Google Style 或 NumPy Style docstrings。
- **导入顺序**: 标准库 -> 第三方库 -> 本地模块 (`vnpy` -> `vnpy_qmt`)。
- **命名**:
  - 类名: `CamelCase` (e.g., `MyStrategy`)
  - 函数/变量: `snake_case` (e.g., `on_tick`, `last_price`)
  - 常量: `UPPER_CASE` (e.g., `EXCHANGE_SSE`)

## 3. 核心开发模式

### 3.1 策略开发 (Strategy)

A 股策略通常继承自 `CtaTemplate` 或自定义策略基类。

- **回调函数**:
  - `on_init`: 初始化加载历史数据。
  - `on_start`: 启动逻辑。
  - `on_tick`: 高频行情驱动 (TICK级别)。
  - `on_bar`: K线驱动 (分钟/日线级别)。
- **下单操作**:
  - 使用 `self.buy()`, `self.sell()` (平仓/卖出), `self.short()`, `self.cover()`。
  - **注意**: A 股为 T+1 制度，且通常只能做多（除非融券）。卖出时需检查 `frozen` 和 `available` 持仓。

### 3.2 扩展网关 (Gateway)

若需修改 `QmtGateway` (`vnpy_qmt/`):

- **MD (Market Data)**: 负责 `subscribe` 和 `on_tick`。注意 `xtquant` 的合约代码格式转换。
- **TD (Trading)**: 负责 `send_order`, `cancel_order` 和 `query_*`。
- **并发**: 网关回调运行在独立线程，更新共享数据需注意线程安全，但推送到 `EventEngine` 后由主线程串行处理，策略层无需加锁。

### 3.3 数据结构

所有交互必须使用 `vnpy.trader.object` 中定义的标准数据类：

- `TickData`: 行情
- `OrderData`: 委托
- `TradeData`: 成交
- `PositionData`: 持仓 (注意区分 `Exchange.SSE` 和 `Exchange.SZSE`)

### 3.4 数据根目录与持久化边界

- 默认部署只配置 `VNPY_DATA_ROOT`，运行态默认路径都从该 root 派生：`state/`、`ml_output/`、`snapshots/`、`models/`、`logs/`、`backups/`。
- ML 日更事实链路固定为 `DailyIngestPipeline -> snapshots/merged + snapshots/filtered + stock_data/by_stock + qlib_data_bin`。任何成功执行到 dump 阶段的 `ingest_today(T)` 都会原子替换 `<VNPY_DATA_ROOT>/qlib_data_bin`，并更新 `calendars/day.txt`，因此 `run_ml_headless.py`、`smoke_full_pipeline.py`、运维手动触发和 20:00 cron 都必须视为会修改当前 qlib provider 的入口。
- `qlib_data_bin` 禁止倒退发布：目标日 `T` 不得早于当前 `calendars/day.txt` 末尾，也不得早于 `<VNPY_DATA_ROOT>/snapshots/merged/daily_merged_*.parquet` 中的最新日期。若已有 2026-05-15 快照，不允许再把 provider 发布成 2026-05-13。
- `ML_INGEST_ALLOW_QLIB_ROLLBACK=1` 只能作为人工确认后的应急回滚逃生开关；不要写入 `.env`、`.env.production`、Windows 服务、计划任务或长期启动脚本。使用时必须记录原因、目标日期和恢复动作，用完立即 unset。正常 smoke/headless/e2e 绝不能依赖该变量。
- 运行 `smoke_full_pipeline.py` 时，如果不希望触碰生产数据源，必须使用隔离的 `VNPY_DATA_ROOT`，或至少显式覆盖 `ML_QLIB_DIR`、`ML_SNAPSHOT_DIR`、`ML_MERGED_PARQUET_PATH` 到临时目录；否则默认会更新生产 root 下的 qlib calendar。
- 通用策略权益事实源固定为 `<VNPY_DATA_ROOT>/state/strategy_equity_journal.db`，读写入口是 `vnpy_common.persistence.strategy_equity_journal`。
- 日终权益 journal 对所有策略引擎生效，不属于 ML 专属能力。回放、模拟实时、真实柜台收盘分别使用 `replay_settle`、`sim_live_settle`、`broker_live_close`，并且必须带 `(engine, strategy_name)`。
- 真实柜台 `broker_live_close` 写入时间统一由 env `VNPY_BROKER_LIVE_EOD_JOURNAL_TIME` 管理，默认 `16:00`；不要在策略或 service 中散落硬编码收盘后触发时间。
- 多策略共享真实账户时，策略级权益归因依赖 `OrderRequest.reference={strategy_name}:{seq}`、`strategy_trade_journal` 成交流水和每策略初始资金配置 `VNPY_STRATEGY_INITIAL_CAPITALS`；不要在 mlearnweb 或前端按账户权益自行拆分。
- webtrader 对外只暴露通用接口 `/api/v1/strategy/equity-journal`；不要再新增或恢复 ML 专属 replay equity API。
- 旧 `vnpy_ml_strategy/replay_history.py`、`REPLAY_HISTORY_DB`、`replay_history.db` 不再作为运行时契约使用；新增脚本、测试、文档不要继续依赖这些旧名。
- vnpy 不直接写 mlearnweb.db。mlearnweb 通过 HTTP 拉取 vnpy 事实源并写自己的展示库，跨工程边界必须保持 HTTP/RPC 解耦。

## 4. 常用命令与操作

- **启动环境**: 确保 Python 环境已安装 `xtquant`。
- **启动脚本**: `python run_sim.py`
- **运行测试**: `python -m pytest tests/` (如有)
- **代码检查**: `flake8 .` 或 `pylint vnpy`

## 5. A 股特有注意事项

- **合约代码**: vn.py 使用 `symbol.exchange` 格式 (e.g., `600000.SSE`)。QMT 使用 `symbol.market` (e.g., `600000.SH`)。需使用 `vnpy_qmt.utils` 进行转换。
- **交易时间**: 09:30-11:30, 13:00-15:00。策略需处理非交易时间段的数据杂波。
- **涨跌停**: `TickData` 中包含 `limit_up` 和 `limit_down`，下单前必须检查价格是否越界。
- **委托回报**: QMT 的回报可能是异步的，且存在轮询机制，策略不应假设下单后立即成交。

## 6. AI Agent 行为准则

- **修改代码前**: 先阅读相关模块的源码，理解现有逻辑。
- **验证**: 修改核心逻辑（如 `on_order`）后，必须进行模拟盘验证。
- **异常处理**: 涉及网络请求或柜台交互的代码，必须包含 `try...except` 块，并通过 `self.write_log` 记录错误堆栈。
- **不要删除**: 除非明确要求，不要删除现有的 `TODO` 或注释。
- 新增代码：新增复杂函数尽量给出函数注释，包括函数功能描述入参说明
- 测试：新增复杂逻辑需进行必要的测试
- 命令执行：对于长时间运行的命令、服务、测试、回放、构建或调试脚本，必须实时关注日志输出和进度反馈；不要只是启动命令后等待固定超时时间。应根据输出及时判断卡住、报错、进入等待、端口占用、依赖缺失或数据路径异常，并尽早中断、调整或汇报，以提高运行调试效率。
- 问题定位：不要猜测问题根因。必须基于日志、测试结果、数据库查询、接口响应、性能指标、复现实验或代码路径分析给出可验证证据，并用量化数据和严密逻辑推理说明根因。
- 修复原则：不要只修表面现象。修复前应先把问题根因调查清楚，说明症状、触发条件、根因链路和影响范围，再提出解决方案；无法完全确认根因时，应明确不确定性并继续补充验证。
- 修改范围：代码修改应遵循最小范围原则，优先选择影响面小、可验证、可回滚的方案。若预计改动量较大、影响模块较多、涉及交易核心链路或可能改变既有行为，必须先提出修改方案、影响评估和验证计划，经过用户批准后再执行。
- 语言：请使用中文进行交流

## 7. 上下文压缩与信息保全规范

为避免长对话触发上下文压缩后丢失关键项目信息，AI Agent 在本项目中应遵循以下规则：

- **重要信息必须落地到文件**：项目目标、关键约束、已确认决策、外部依赖、柜台/行情连接配置、数据路径、风险点、测试命令、禁止修改范围等，不应只保存在聊天上下文中。
- **长期规则写入 `AGENTS.md`**：跨任务长期有效的规范、架构边界、编码约定和协作方式，应维护在本文件中。
- **阶段状态写入工作日志**：复杂任务或多轮任务应创建/更新 `WORKLOG.md`，记录当前进度、已完成事项、关键结论、待办项、阻塞点和下一步。
- **复杂改动先写计划**：涉及交易核心逻辑、网关、订单/成交/持仓处理、风控或多模块联动时，应创建/更新 `IMPLEMENTATION_PLAN.md`，包含分阶段步骤、影响文件、验收标准、风险与回滚思路。
- **交接摘要及时沉淀**：当任务跨度较长、上下文接近压缩、或准备暂停时，应在 `WORKLOG.md` 中追加 handoff 摘要，说明当前状态、最近修改、未完成事项和建议下一步。
- **长日志不要直接堆入对话**：大量测试输出、模拟盘日志、柜台回报、完整 diff、长数据样本应优先保存为文件或只提炼关键错误；对话中只汇报结论、关键路径和必要片段。
- **恢复上下文时先读项目文件**：新一轮工作开始时，优先读取 `AGENTS.md`、`WORKLOG.md`、`IMPLEMENTATION_PLAN.md`、`architecture.md` 以及相关模块源码，再继续实现。

## 8. Git 提交信息风格约束

本仓库历史中 Claude Code 提交信息质量较高，后续 AI Agent 提交应尽量沿用以下风格：

- **标题使用 Conventional Commit**：格式为 `type(scope): 摘要`，常用 `feat` / `fix` / `docs` / `chore` / `refactor` / `test`；scope 应指向实际模块，如 `headless-runtime`、`webtrader`、`signal-strategy-plus`、`deploy`、`bootstrap`、`submodule`。
- **标题要说明真实交付物**：优先写清“修复了什么/新增了什么/推进了哪个阶段”，必要时在括号中补充阶段、业务场景或关键症状，例如 `fix(fastfail): reduce readonly spinner on node disconnect`。
- **正文先讲背景和根因**：复杂提交必须说明问题现象、根因和影响范围，尤其是实盘页面、webtrader、QMT 网关、headless、回放、订单/成交/持仓链路相关改动。
- **正文再列本次包含**：用简短 bullet 列出关键改动、涉及模块、接口、脚本、配置或数据契约；每条写结果和影响，不写空泛描述。
- **写清预期收益**：对用户体验、部署稳定性、实盘安全、数据同步、前端展示、回放一致性等有改善的提交，应明确说明收益。
- **写清验证结果**：提交正文应包含实际执行过的测试、构建、smoke、sim_e2e、接口调用或手工验证结果；如果未验证或只做部分验证，必须如实说明。
- **记录风险与注意**：对交易核心逻辑、端口、配置、数据路径、柜台连接、超时策略、策略 reference、子模块指针等有影响的提交，应写明风险、兼容性和后续注意事项。
- **区分本次提交与工作区杂项**：如果仓库中存在未提交的本地配置、日志、测试产物或用户改动，提交正文应说明“未纳入本提交”的范围，且不要顺手提交无关文件。
- **子模块提交要写指针来源和内容摘要**：`chore(submodule): bump ...` 应说明新指针对应的下游能力、修复、契约变化和验证状态。
- **不要伪造协作者尾注**：只有实际由对应工具/人员共同完成且用户认可时，才添加 `Co-Authored-By`；不要为了模仿历史风格伪造 Claude 或其他身份。

推荐正文结构：

```text
背景/问题:
- ...

本提交包含:
- ...

预期收益:
- ...

验证:
- ...

风险与注意:
- ...
```

## 9. 软件模块开发文档规范

`vnpy_ml_strategy/docs/` 与 `vnpy_ml_strategy/test/README.md` 是本仓库模块文档的参考样板。后续新增或重构重要 app / 子模块时，应按软件行业最佳实践补齐同等级文档，保证架构、开发、部署、运维、测试和使用方式都能被后续 AI Agent 与工程师快速接手。

### 9.1 推荐文档结构

每个重要模块建议维护以下文档，路径可按模块实际情况调整：

| 文档 | 目的 | 必写内容 |
|---|---|---|
| `docs/README.md` | 文档入口与导航 | 模块定位、顶层全景图、文档索引、核心组件总览、快速入门、关键路径、跨工程依赖 |
| `docs/architecture.md` | 架构原理 | 进程/服务拓扑、数据流、控制流、关键解耦设计、核心类关系、跨进程/跨服务通信 |
| `docs/developer.md` | 开发扩展 | 代码结构、扩展点、新增策略/网关/指标流程、测试分层、调试技巧、常见陷阱 |
| `docs/deployment.md` | 部署上线 | 服务器规格、依赖安装、配置文件、路径约定、启动顺序、服务化、部署后验收 checklist |
| `docs/operations.md` | 运行维护 | 日常监控、日志位置、故障排查、备份恢复、升级流程、已知不足、应急路径 |
| 专题文档 | 复杂功能专题 | 对双轨、回放、风控、数据同步、信号链路等重点功能单独成文，讲清设计取舍和使用方法 |
| `test/README.md` | 测试体系入口 | 测试分层、文件总览、数据流/依赖图、关键约定、逐测试说明、运行命令、验收标准 |

### 9.2 图表优先原则

文档应充分使用图表帮助读者建立空间感和时序感。能用图表讲清的内容，优先用图表；文字用于解释设计意图、边界和例外。

- **架构图/拓扑图**：说明进程、服务、数据库、外部系统、部署节点和网络边界。
- **数据流图**：说明训练产物、行情数据、信号、订单、成交、持仓、监控指标在模块间如何流转。
- **流程图**：说明启动、回放、推理、调仓、同步、告警、恢复等关键流程。
- **时序图**：说明 cron、异步回调、HTTP/RPC 调用、订单生命周期、跨进程协作的先后关系。
- **类图/组件图**：说明核心类继承、组合关系、接口边界和扩展点。
- **表格**：用于文件总览、配置项、环境变量、端口、路径、测试用例、故障现象与处理方法。
- **checklist**：用于部署、升级、验收、回滚、应急处置等需要逐项确认的流程。

图表优先使用 Mermaid；不适合 Mermaid 的内容可用 Markdown 表格或 ASCII 图。图表下方必须配简短说明，指出关键路径、边界和读图要点。

### 9.3 重点关键功能必须详细说明

对交易、推理、数据同步、风控、回放、部署、监控等关键功能，不能只写“做了什么”，必须写清：

- **背景/问题**：为什么需要这个功能，解决什么真实问题。
- **设计目标**：正确性、稳定性、可观测性、性能、兼容性、安全性等目标。
- **核心流程**：入口、主要步骤、关键状态、输出结果，必要时给流程图或时序图。
- **关键数据结构/契约**：配置字段、数据库表、文件格式、HTTP/RPC API、消息字段、路径约定。
- **边界与不变量**：哪些模块可以调用，哪些不能调用；哪些状态必须保持一致；哪些路径/端口/命名不可随意改。
- **失败模式与降级策略**：超时、断连、脏数据、重复执行、部分失败、回放中断、数据库锁等如何处理。
- **验证方式**：单测、集成测试、e2e、smoke、手工验收命令和通过标准。
- **风险与运维注意**：线上影响、回滚方式、监控指标、日志位置、常见误用。

### 9.4 测试文档规范

测试说明通常放在对应模块的 `test/README.md` 或 `tests/README.md`。应参考 `vnpy_ml_strategy/test/README.md` 的写法，把测试目录当成“验证体系入口”，而不是简单文件列表。

- 开头说明测试目录覆盖的模块范围、验证目标和读者应该从哪里开始。
- 用表格按层级归类：单元测试、集成测试、e2e 等价验证、smoke、诊断脚本、fixture、输出目录。
- 给出数据流/文件依赖图，说明测试输入、产物、临时目录、数据库和外部服务之间的关系。
- 写清关键约定：Python 解释器、环境变量、数据源、测试数据路径、端口、外部依赖和不允许使用的历史数据源。
- 对重点测试逐条说明目的、验收条件、运行命令、测试原理和失败时的诊断方法。
- 对诊断脚本和出图脚本说明输入、输出、典型使用场景和产物位置。
- 对慢测试、实盘相关测试、需要盘中环境的测试，明确触发条件、风险和跳过策略。

### 9.5 写作要求

- 文档面向“接手维护的人”和“未来的 AI Agent”，不要只服务当前作者的短期记忆。
- 先给全景，再给细节；先讲为什么，再讲怎么做；先讲主路径，再讲异常路径。
- 命令、路径、端口、环境变量、配置键必须可复制；示例输出要包含关键成功标志。
- 涉及真实交易、实盘账户、凭证、生产路径的内容必须明确安全边界，不提交真实密钥。
- 文档与代码变更要同步：新增关键功能、配置项、部署步骤、测试脚本或数据契约时，必须同时更新对应文档。
- 文档不追求堆字数，追求可恢复上下文、可验证、可操作、可维护。

## SignalStrategyPlus v2 信号链路长期规则（2026-05-17）

- `stock_trade` 不再作为 `vnpy_signal_strategy_plus` 的运行时信号契约；不要为新策略、新测试或新脚本继续读取/写入 `stock_trade.processed`。
- JoinQuant/CSV/测试注入信号必须进入 `trade_signal_events`，并以 `strategy_signal_applications` 记录每个 `(account_id, gateway_name, engine, strategy_name, signal_event_id)` 的消费状态。
- `pct` 字段语义固定为 `trade_value_pct_of_total_portfolio`，表示本次交易金额占聚宽组合总资产比例；不要把它解释成目标权重、可用现金比例或当前持仓比例。
- Redis Stream 只是传输层；模拟账户重建和审计以 MySQL v2 signal journal 为准，不以 Redis backlog 为事实源。
- QMT_SIM 重启恢复必须依赖 `<VNPY_DATA_ROOT>/state/sim_<account>.db`，其中 `sim_meta` 保存 order/trade 计数、`last_settle_date` 和 `today_buy_json`；策略启动时应从持久化订单 reference 恢复 `_order_seq`。
- `run_signal_dual_track_demo.py` 是 SignalStrategyPlus 近实盘启动入口；未显式传 `--settle-through` 时，应在 runner 层按本地交易日历默认结算到最近已完成交易日，不要在策略类里写全局 runtime override 或自动推断逻辑。
- v2 历史批量回放期间，replay adapter 必须把策略变量 `replay_status` 标记为 `running`，空闲/退出后恢复 `idle`，避免通用 `sim_live_settle` journal 在回放中途采样并污染权益曲线。
