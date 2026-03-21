# MySQL 信号策略接入 CTA 框架（Signal Strategy Plus）Spec

## Why
当前 `MultiStrategySignalStrategy` 依赖 `vnpy_signal_strategy` 的 `SignalTemplate/SignalEngine`，无法直接在 CTA 引擎/回测器中加载与运行。需要在不删减功能（除 `on_tick/on_data` 可由你自改）前提下，把该策略以 CTA 策略的形式接入，并解决与原 `vnpy_signal_strategy` 的命名冲突。

## What Changes
- 将 `vnpy_signal_strategy_plus` 作为“基于 CTA 框架的信号策略 App”进行可运行化修复：
  - 修复策略加载路径，确保从 `vnpy_signal_strategy_plus/strategies` 加载策略类
  - 为 App 设置与 `vnpy_ctastrategy` 不冲突的 `APP_NAME/display_name`（避免同时安装时冲突）
- 将 MySQL 轮询信号策略改造为 CTA 可加载策略基类：
  - 提供 CTA 兼容的策略基类 `MySQLSignalStrategyPlus`（保留：外部 JSON 配置、DB 连接/轮询、信号处理、下单、自动重挂）
  - 提供策略实现 `MultiStrategySignalStrategyPlus`（由 `MultiStrategySignalStrategy` 重命名）
- 引擎能力补齐：
  - 增加可选 `on_timer` 回调的驱动（注册 `EVENT_TIMER`），用于重挂队列节流与信号队列落地执行
  - 增加 CTA 引擎“按 vt_symbol 下单”的公共接口，以支持单策略处理多标的信号（不破坏现有 CTA 策略接口）
- 清理/修正导入与导出命名：
  - `vnpy_signal_strategy_plus` 内部不再引用 `vnpy_signal_strategy.*`
  - 策略类名、导出名称与 `vnpy_signal_strategy` 中同名对象不冲突（重点：策略类、可被加载的基类）
- **BREAKING**：若同时安装 `vnpy_ctastrategy` 与 `vnpy_signal_strategy_plus`，`vnpy_signal_strategy_plus` 的 `APP_NAME` 将变更为新名称（避免冲突）。

## Impact
- Affected specs:
  - 策略加载（扫描 strategies/）
  - 实盘引擎事件处理（新增 timer 驱动）
  - 下单接口（新增按 vt_symbol 下单的扩展接口）
  - 回测（确保策略可被回测器加载运行；多标的回测能力标注约束）
- Affected code:
  - `vnpy_signal_strategy_plus/engine.py`
  - `vnpy_signal_strategy_plus/template.py`
  - `vnpy_signal_strategy_plus/base.py`
  - `vnpy_signal_strategy_plus/__init__.py`
  - `vnpy_signal_strategy_plus/mysql_signal_strategy.py`
  - `vnpy_signal_strategy_plus/auto_resubmit.py`
  - `vnpy_signal_strategy_plus/strategies/multistrategy_signal_strategy.py`（或新增 *_plus.py）

## ADDED Requirements
### Requirement: CTA 可加载的 MySQL 信号策略基类
系统 SHALL 提供 `MySQLSignalStrategyPlus`，可被 `vnpy_signal_strategy_plus` 的 CTA 引擎扫描并加载，且保留原策略的功能接口与行为：
- 外部 JSON 配置加载（按 `strategy_name`/`default` 匹配）
- MySQL 连接与轮询（独立线程轮询）
- 信号处理与下单（包含比例仓位计算、价格/委托类型处理）
- 订单回报驱动的自动重挂（撤单/拒单触发、按间隔节流）

#### Scenario: 实盘启动轮询并能提交委托
- **WHEN** 策略被添加到引擎并启动
- **THEN** 策略进入 trading 状态并启动轮询线程
- **AND** 新信号入队后能在主线程触发下单（避免直接从子线程触发引擎内部状态修改）
- **AND** 订单回报能路由回策略的 `on_order`

### Requirement: Timer 驱动能力
系统 SHALL 在 `vnpy_signal_strategy_plus` 的 CTA 引擎中注册 `EVENT_TIMER` 并驱动策略级 `on_timer`（若存在），用于：
- 执行 `AutoResubmitMixin` 的节流重挂逻辑
- 将轮询线程产生的信号队列在主线程中取出并处理（可配置每次处理条数）

#### Scenario: 触发重挂
- **WHEN** 订单状态变为 CANCELLED/REJECTED 且仍有剩余未成交
- **THEN** 策略登记重挂任务
- **AND** 在后续 timer tick 中按 `resubmit_interval` 节流提交新委托

### Requirement: 命名冲突规避
系统 SHALL 避免与 `vnpy_signal_strategy` 的策略/类名冲突：
- `MultiStrategySignalStrategy` 重命名为 `MultiStrategySignalStrategyPlus`
- `MySQLSignalStrategy` 重命名为 `MySQLSignalStrategyPlus`
- 策略扫描加载时展示的 class name 为 `*Plus` 结尾

#### Scenario: 同时安装两个 App
- **WHEN** 工程同时包含 `vnpy_signal_strategy` 与 `vnpy_signal_strategy_plus`
- **THEN** 不出现同名策略类导致的策略选择/加载混淆

## MODIFIED Requirements
### Requirement: 策略加载源
`vnpy_signal_strategy_plus` 的 CTA 引擎 SHALL 从自身 `strategies/` 目录加载策略类，而不是从 `vnpy_ctastrategy.strategies` 加载。

### Requirement: 订单提交扩展
CTA 引擎 SHALL 提供“指定 vt_symbol 下单”的扩展接口，以支持信号策略按信号中的标的下单，同时保持现有 `send_order(strategy, ...)` 接口对普通 CTA 策略不变。

## REMOVED Requirements
（无）

