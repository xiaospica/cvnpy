# 阶段计划：通用策略权益 Journal 与 mlearnweb 可重建链路

## 背景

当前权益历史存在两套语义混在一起的问题：

- `vnpy_ml_strategy/replay_history.py` 最初为 ML 回放权益服务，但后来被 SignalStrategyPlus、webtrader、mlearnweb 同步链路复用，已经不再是 ML 专属。
- 新增的日终权益 journal service 目前挂在 `MLEngine` 下，只覆盖 ML 策略；SignalStrategyPlus、CTA 等其它策略引擎仍无法自动获得 live/sim-live 日终权益事实。
- mlearnweb 的实盘页面应该可从 vnpy 事实源重建，而不是依赖 mlearnweb 本地高频 snapshot 是否完整。

本轮不做向后兼容。工程仍在开发阶段，可以直接替换旧命名、旧 DB 和旧 API，避免留下长期包袱。

## 架构决策

- vnpy 侧事实源统一放到 `vnpy_common/persistence/`。
- webtrader 只作为 HTTP/WS 门面读取并暴露事实源，不拥有事实数据写入职责。
- 策略权益 journal 使用新库：`<VNPY_DATA_ROOT>/state/strategy_equity_journal.db`。
- 权益记录身份必须包含 `engine + strategy_name + source_label + ts`，避免不同策略引擎同名策略互相覆盖。
- source 保留真实来源，不再统一伪装成 `replay_settle`：
  - `replay_settle`
  - `sim_live_settle`
  - `broker_live_close`
- mlearnweb 只通过 webtrader API 同步 journal，不 import vnpy，也不直接读 vnpy 文件系统。

## 优先级

### P0 通用持久化层

- 新增 `vnpy_common/persistence/strategy_equity_journal.py`。
- 新增 `vnpy_common/persistence/event_journal.py`，承接原 `vnpy_webtrader/event_journal.py` 的职责。
- 更新 `vnpy_common/data_paths.py`：
  - 新增 `strategy_equity_journal_db_path()`。
  - 移除 `replay_history_db_path()` 默认入口。
- 删除或停止使用 `vnpy_ml_strategy/replay_history.py`。
- 所有回放权益写入改用 `strategy_equity_journal.write_snapshot()`。

### P1 通用日终权益服务

- 新增 `vnpy_common/services/strategy_equity_journal_service.py`。
- 将 `vnpy_ml_strategy/services/eod_equity_journal.py` 的逻辑迁入 common service。
- service 支持多引擎注册 strategy provider。
- 接入：
  - `vnpy_ml_strategy.engine.MLEngine`
  - `vnpy_signal_strategy_plus.engine.SignalEnginePlus`
- 统一写入 `engine`、`source_label` 和 `raw_variables`。

### P2 webtrader 与 mlearnweb 同步

- webtrader 增加通用接口：
  - `GET /api/v1/strategy/equity-journal`
- 删除旧 ML 专用 replay equity API：
  - `/api/v1/ml/strategies/{name}/replay/equity_snapshots`
- mlearnweb 将 `replay_equity_sync_service` 改造为 `strategy_equity_journal_sync_service`。
- 同步时按 `node_id + engine + strategy_name + source_label + DATE(ts)` upsert 到 `strategy_equity_snapshots`。
- 事件发布仍使用 `strategy.equity.changed`，但 reason 改成 `strategy_equity_journal_sync`。

### P3 清理与测试

- 清理脚本、测试、文档中 `replay_history.db` / `REPLAY_HISTORY_DB` / `replay_equity_sync` 的旧命名。
- 更新/新增测试：
  - common journal SQLite 单测。
  - common day-end service 单测。
  - ML replay 写入测试。
  - SignalStrategyPlus replay 写入测试。
  - webtrader 新 API 测试。
  - mlearnweb sync service 测试。
- 运行核心回归：
  - `F:\Program_Home\vnpy\python.exe -m pytest vnpy_ml_strategy/test/test_strategy_equity_journal.py -q`
  - `F:\Program_Home\vnpy\python.exe -m pytest vnpy_webtrader/test -q`
  - `E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe -m pytest mlearnweb/tests/test_backend -q`

### P4 文档与长期规则

- 丰富 `docs/architecture.md`：
  - vnpy 事实源与 mlearnweb 可重建架构。
  - event journal 与 strategy equity journal 的职责边界。
  - 策略权益数据流和失败模式。
  - 多策略共享账户、真实柜台总资产口径、SQLite WAL 等风险。
- 更新 `AGENTS.md`：
  - 标注架构文档路径 `docs/architecture.md`。
  - 规定通用持久化放 `vnpy_common/persistence`。
  - 规定 webtrader 不拥有事实数据写入职责。
  - 规定 mlearnweb 不 import vnpy、不直读 vnpy 文件。

## 风险

- 旧 API 删除后，所有调用方必须同步改造，否则会直接失败。
- 真实柜台 `balance` 字段语义可能因柜台而异，需要在 service 中优先使用总资产口径或明确标注来源。
- 多策略共享一个真实账户时，账户级权益无法天然精确归因到单策略；当前 journal 只能提供账户级事实或策略变量提供的权益。
- SQLite 写入失败必须只记录 warning，不应阻断交易主循环。
- mlearnweb sync 如果只按 `ts` 增量，可能漏掉同一 ts 的修正；后续如需要强一致增量，应在 mlearnweb 存远端 `seq`。

## 验收标准

- ML 与 SignalStrategyPlus 都能向 `strategy_equity_journal.db` 写入权益点。
- webtrader 新接口能按 engine、strategy、source、since 返回权益 journal。
- mlearnweb 能从新接口同步并展示历史权益曲线。
- 删除旧 `replay_history.py` 与旧 ML replay equity API 后，核心测试仍通过。
- `docs/architecture.md` 和 `AGENTS.md` 已记录新架构边界。

## 当前执行结果（2026-05-16）

- P0-P2 代码重构已完成：common journal、common day-end service、webtrader 通用接口、mlearnweb 同步 loop 均已切换到新契约。
- P3 已覆盖核心单测：common journal、ML 模板回放写入、QMT_SIM 回放控制器、mlearnweb journal sync。
- P4 已更新：`docs/architecture.md`、vnpy `AGENTS.md`、qlib/mlearnweb `AGENTS.md`。

## 补充收口（2026-05-16）

- webtrader 已补 `GET /api/v1/strategy/equity-journal` 的 HTTP route 测试，覆盖 query 透传与 RPC error unwrap。
- 真实柜台 `broker_live_close` 写入时间改为 env `VNPY_BROKER_LIVE_EOD_JOURNAL_TIME`，默认 `16:00`。
- 多策略共享真实账户时，QMT 网关持久化 `OrderRequest.reference` 到 broker order id 的映射，并将成交写入 `strategy_trade_journal`；`StrategyEquityJournalService` 使用每策略初始资金和成交流水归因 `broker_live_close.strategy_value`，同时保留真实账户总权益到 `account_equity`，缺失条件时显式回退账户级权益。
- 当前仍需实盘环境长期观察：券商持仓市值字段口径、手续费/税费字段缺失、策略初始资金配置变更后的历史口径。

## 补充计划：JoinQuant 信号 v2 journal 与模拟账户可重建（2026-05-17）

### 背景/问题

- `stock_trade` 的 `processed` 是全局状态，不区分账户、网关、策略实例和消费方；多个策略共享同表时，重启、重放或双轨模拟都容易互相污染。
- 聚宽侧 `pct` 的真实语义是“本次交易金额 / 组合总资产”，不是目标权重；旧表没有显式语义字段，后续维护容易误解。
- QMT_SIM 虽已持久化账户、持仓、订单和成交，但 `order_count`、`trade_count`、`last_settle_date`、当日买入跟踪和 reference seq 恢复不足，vnpy 重启后存在 ID 复用、T+1 状态缺口和 reference 串号风险。

### 决策

- 不再支持旧 MySQL 信号消费链路：`stock_trade` 不作为 SignalStrategyPlus 的运行时契约。
- 新事实表为 `trade_signal_events`，消费 checkpoint 为 `strategy_signal_applications`。
- JoinQuant/CSV/测试注入都必须携带或生成稳定 `signal_uid`，并显式写入 `pct_semantics=trade_value_pct_of_total_portfolio`。
- Redis 继续只作为传输层；MySQL v2 signal journal 是重建与审计事实源。
- QMT_SIM 的 sim DB 继续作为模拟账户事实源，新增 `sim_meta` 保存运行时元数据。

### 优先级

- P0：v2 信号表、bridge、策略消费、JQ payload、QMT_SIM runtime metadata、reference seq 恢复。
- P1：基于 v2 信号源做新模拟账户快速重建，健康 sim DB 只做补结算和 catch-up。
- P2：状态 doctor、人工注入、EOD 快照加速。
- P3：更新所有旧 `stock_trade` 文档/脚本残留，补全 HTTP e2e。

### 验收标准

- Redis bridge 只写 `trade_signal_events`，不写 `stock_trade`。
- `MySQLSignalStrategyPlus` 只消费 v2 signal journal，并按账户/网关/引擎/策略写 checkpoint。
- QMT_SIM 重启后恢复订单/成交计数、上次结算日、当日买入跟踪，并能从历史 reference 恢复最大序号。
- 核心单测通过：signal journal、QMT_SIM persistence。
