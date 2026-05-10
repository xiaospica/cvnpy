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
