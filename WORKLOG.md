# WORKLOG

## 2026-05-16 — run_ml_headless 清库后 replay 结果不一致

结论:
- 已复现用户反馈：两轮 `reset_sim_state.py --all` 后运行 `run_ml_headless.py`，修复前 `selections.parquet` / `metrics.json` 一致，但 `replay_settle` 权益值不同。
- 根因是 ML replay 调仓读取当前持仓时走 `MainEngine/OmsEngine` 异步缓存；快速回放里 sim counter 已同步更新，但 OMS 事件可能落后一拍，导致同一信号生成不同交易路径。
- 本机推理 Python 还存在 polars 导入问题：`RuntimeError: unknown feature flag: 'sse3'`，需要给推理子进程跳过误报的 CPU 检查。

已修改:
- `vnpy_ml_strategy/template.py`：sim gateway 下 `_get_long_positions()` 直接读取 `gateway.td.counter.positions`，实盘仍走 OMS fallback。
- `vnpy_ml_strategy/predictors/qlib_predictor.py`：推理子进程默认注入 `POLARS_SKIP_CPU_CHECK=1`。
- `vnpy_common/services/strategy_equity_journal_service.py`：跳过 `replay_status=running` 策略，避免显式 replay 期间写入不稳定的 `sim_live_settle` 抽样点。

验证:
- 两轮清库自测：`run_selftest.py --runs 2 --timeout 1200` -> MATCH；`replay_settle` 134 行、预测/选股/指标产物 402 个，两轮 SHA256 都是 `f85d60fc4e36a752cd8eac77db0bc1ac20948e598d4b79748fe0e211174bffb9`。
- `F:/Program_Home/vnpy/python.exe -m pytest vnpy_common/test/test_strategy_equity_journal_service.py vnpy_ml_strategy/test/test_ml_strategy_replay.py -q --basetemp .pytest_tmp_replay_fix` -> 23 passed。

## 2026-05-16 通用策略权益 Journal 重构启动

### 已确认结论

- 日终权益 journal 必须是 vnpy 节点级通用事实源，不能只挂在 `vnpy_ml_strategy`。
- webtrader 只负责读取并暴露事实源，不负责拥有事实数据写入。
- 通用持久化统一放到 `vnpy_common/persistence/`。
- 新权益库使用 `<VNPY_DATA_ROOT>/state/strategy_equity_journal.db`。
- 不做向后兼容：旧 `replay_history.db`、旧 `REPLAY_HISTORY_DB` 默认入口、旧 ML replay equity API 都按新契约替换。

### 本轮计划

- 已新增 `IMPLEMENTATION_PLAN.md`，按 P0-P4 拆分通用持久化、通用 service、webtrader/mlearnweb 同步、测试和文档。
- 第一阶段先实现 P0/P1：`strategy_equity_journal` 存储与通用日终权益 service。
- 第二阶段改造 webtrader 与 mlearnweb sync，删除旧 ML 专用接口。

### 注意事项

- 当前工作区已有用户迁移文档造成的 `docs/architecture.md` 新文件与旧根目录文档删除，本轮只在此基础上增量补充。
- 当前仍有两个本地策略 JSON 改动，不纳入 journal 重构：
  - `vnpy_signal_strategy_plus/mysql_signal_setting.json`
  - `vnpy_signal_strategy_plus/test/redis_live_sim_setting.json`

### 2026-05-16 进展更新

- 已将权益 journal 存储迁入 `vnpy_common/persistence/strategy_equity_journal.py`，DB 路径为 `<VNPY_DATA_ROOT>/state/strategy_equity_journal.db`。
- 已将原 ML 专属日终权益 service 迁入 `vnpy_common/services/strategy_equity_journal_service.py`，并接入 `vnpy_ml_strategy` 与 `vnpy_signal_strategy_plus` 两个策略引擎。
- 已将 webtrader 对外接口切换为 `/api/v1/strategy/equity-journal`，旧 ML replay equity endpoint 不再作为运行时契约。
- 已将 mlearnweb 同步 loop 切换为 `strategy_equity_journal_sync_service`，按 `(node_id, engine, strategy_name, source_label)` 增量拉取 `replay_settle`、`sim_live_settle`、`broker_live_close`。
- 已更新 `docs/architecture.md` 和 `AGENTS.md`，明确 vnpy 不直接写 mlearnweb.db、mlearnweb 只通过 webtrader 拉取事实源。

待验证：
- `vnpy_common/test/test_strategy_equity_journal.py`
- `vnpy_ml_strategy/test/test_template_replay_persist.py`
- `vnpy_qmt_sim/test/test_sim_replay_controller.py`
- `mlearnweb/tests/test_backend/test_strategy_equity_journal_sync.py`

验证结果：
- `E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe -m pytest tests/test_backend/test_strategy_equity_journal_sync.py -q`：8 passed。
- `F:\Program_Home\vnpy\python.exe -m pytest vnpy_common/test/test_strategy_equity_journal.py vnpy_ml_strategy/test/test_template_replay_persist.py vnpy_qmt_sim/test/test_sim_replay_controller.py -q`：12 passed。
- `F:\Program_Home\vnpy\python.exe -m py_compile ...` 覆盖 webtrader/common/qmt_sim/脚本核心改动：通过。
- `E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe -m py_compile ...` 覆盖 mlearnweb live_main/client/sync/live_trading_service：通过。

### 2026-05-16 补充收口：webtrader e2e 与 broker-live 精确归因

已处理：
- `broker_live_close` 写入触发时间从硬编码收敛到 `VNPY_BROKER_LIVE_EOD_JOURNAL_TIME`，默认 `16:00`。
- 新增 `strategy_trade_journal`，在 QMT 真实柜台路径记录 `OrderRequest.reference` 与 broker order id 的映射，并持久化成交归属。
- `StrategyEquityJournalService` 在多策略共享真实账户时，优先用每策略初始资金 + 成交流水 + 收盘持仓市值计算 `strategy_value`，并保留真实账户总权益到 `account_equity`；条件不足时回退到账户级权益并在 raw_variables 标记 fallback。
- 新增 webtrader `/api/v1/strategy/equity-journal` HTTP route 测试，覆盖 query 透传与 RPC 错误解包。
- 已更新 `.env.example`、`AGENTS.md`、`IMPLEMENTATION_PLAN.md`、`docs/architecture.md`，记录 env 配置、归因原理与风险边界。

验证结果：
- `F:\Program_Home\vnpy\python.exe -m py_compile vnpy_common\services\strategy_equity_journal_service.py vnpy_common\persistence\strategy_trade_journal.py vnpy_qmt\td.py vnpy_webtrader\test\test_strategy_equity_journal_http.py vnpy_common\test\test_strategy_equity_journal_service.py`：通过。
- `F:\Program_Home\vnpy\python.exe -m pytest vnpy_common\test\test_strategy_equity_journal_service.py vnpy_common\test\test_strategy_equity_journal.py vnpy_ml_strategy\test\test_template_replay_persist.py vnpy_webtrader\test\test_strategy_equity_journal_http.py -q`：15 passed，1 个 pytz 第三方 deprecation warning。

风险与注意：
- 当前 `TradeData` 没有手续费/印花税字段，真实柜台精确归因的现金暂不扣交易费用，已写入 `raw_variables.fee_note`。
- 精确归因依赖策略订单 reference；手工下单或旧订单没有 reference 时不会被分配到策略。
- `VNPY_STRATEGY_INITIAL_CAPITALS` 是共享账户精确归因的关键配置，key 支持 `gateway:engine:strategy`、`engine:strategy`、`strategy`。

## 2026-05-12 RedisLiveSim v2 前端收益率为 0

背景：
- 用户使用 `run_signal_dual_track.py --mode v2 --source-stg harvester_micro_cap_1 --shadow-stg harvester_micro_cap_1_shadow` 联合聚宽回测写 Redis 信号。
- 策略实际已成交，sim DB 中有订单和成交，但 mlearnweb 前端收益率显示为 0。

关键结论：
- 策略交易链路正常，`sim_QMT.db` 与 `sim_QMT_SIM_redis_shadow.db` 均有成交。
- 前端收益率依赖 mlearnweb `strategy_equity_snapshots` 中的历史权益曲线；当只有当前实时 `account_equity` 单点时，收益率无法计算，会显示 0 或空。
- 本次根因是路径不一致：脚本加载 `.env.production` 后 `QS_DATA_ROOT=C:/Users/Administrator/Downloads/vnpy_data`，但 RedisLiveSim 本地配置的 sim DB 目录是 `D:/vnpy_data/state`。WebTrader 按 `QS_DATA_ROOT/state/replay_history.db` 查回放快照，因此读不到实际补写在 `D:/vnpy_data/state/replay_history.db` 的权益点。

已处理：
- 已将本次回放权益点从 `D:/vnpy_data/state/replay_history.db` 补入 `F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db`，两条策略各 24 条 `replay_settle`。
- mlearnweb performance-summary 已验证：
  - `harvester_micro_cap_1` 当前累计收益约 `+12.03%`，样本数 25。
  - `harvester_micro_cap_1_shadow` 当前累计收益约 `+12.03%`，样本数 25。
  - 纯回放最后交易日 `2026-05-08` 权益为 `1,105,475`，约 `+10.55%`。
- 已修改 `run_signal_dual_track.py`：启动时将 `REPLAY_HISTORY_DB` 固定到当前 sim `db_dir/replay_history.db`，避免 `.env.production` 的 `QS_DATA_ROOT` 把 WebTrader 指到错误位置。
- 已修改 `vnpy_qmt_sim/replay/controller.py`：当回放快照写入返回 False 时，将目标 DB 路径写入策略日志，便于后续定位。

验证：
- `F:/Program_Home/vnpy/python.exe -m py_compile run_signal_dual_track.py vnpy_qmt_sim/replay/controller.py`
- mlearnweb DB 查询确认两个策略均有 `replay_settle=24` 行。
- `GET http://127.0.0.1:8100/api/live-trading/strategies/local/SignalStrategyPlus/{name}/performance-summary` 返回非零累计收益。

注意：
- 当前运行中的 v2 进程不会自动加载这次脚本修改；下次重启 `run_signal_dual_track.py` 后生效。
- 如果使用默认清理启动，会删除本次 demo 相关 sim DB / replay_history 快照，需要重新跑聚宽回测或使用 `--no-cleanup` 保留已有状态。

## 2026-05-12 聚宽回测 vs 本地 v2 复现对账

背景：
- 用户运行 bridge、`run_signal_dual_track.py --mode v2 --source-stg harvester_micro_cap_1 --shadow-stg harvester_micro_cap_1_shadow`，并在聚宽上开启回测写 Redis 信号。
- 聚宽产物目录为 `C:/Users/richard/Downloads/jqreplay_mc1`，包含 `result_1.csv`、`transaction (1).zip`、`position.zip`、`log.zip`。

已产出：
- 对账目录：`artifacts/jqreplay_mc1_compare/`
- 中文报告：`artifacts/jqreplay_mc1_compare/REPORT_CN.md`
- 明细 CSV：权益、交易、最终持仓、日级持仓、源/影子成交一致性等。

关键结论：
- v2 源策略与影子策略内部完全一致：111/111 行成交逐行一致，源/影子 `replay_settle` 最大权益差为 0。
- 本地模拟柜台没有完全复现聚宽回测：聚宽有效成交 111 行，本地也 111 行，date+symbol+side 全部匹配，但 69/111 个共同键股数不一致，平均绝对股数差 78.38 股，最大 400 股。
- 最终权益聚宽 `1,112,025.37`，本地 `1,120,332.97`，本地高 `8,307.60`。
- 最终持仓标的集合完全一致 15/15，但 7 个标的股数不一致，总绝对股数差 1,300 股。
- 根因判断为执行/撮合口径差异：聚宽记录实际成交价/股数，本地 QMT_SIM 基于本地行情、资金和整手约束重新撮合。

## 2026-05-17 — SignalStrategyPlus v2 信号 journal 与 QMT_SIM 重启恢复

已确认：
- 不再兼容旧 `stock_trade` 信号消费链路；其它历史表不作为本链路事实源。
- `pct` 固定为 `trade_value_pct_of_total_portfolio`，必须显式写入 payload/table。
- Redis 是传输层，MySQL `trade_signal_events` + `strategy_signal_applications` 是信号重建和审计事实源。

已修改：
- 新增 `vnpy_signal_strategy_plus/signal_journal.py`，定义 `trade_signal_events`、`strategy_signal_applications`、payload normalization、幂等 upsert 和 checkpoint helper。
- `jq_redis_trade.py` 生成稳定 `signal_uid`、`source_signal_id`、`pct_semantics`、`amt`，并仅使用 Redis Stream。
- `redis_to_mysql_bridge.py` 只写 v2 signal journal，不再写 `stock_trade`。
- `MySQLSignalStrategyPlus` 改为查询 v2 未消费信号，并按策略 scope 写消费 checkpoint。
- 回放 adapter、CSV replay、live order test、E2E cleanup/wait、purge 脚本切到 v2 表。
- QMT_SIM `sim_meta` 保存 `order_count`、`trade_count`、`last_settle_date`、`today_buy_json`；gateway 启动恢复这些元数据。
- `SignalTemplatePlus` 与 `vnpy_ml_strategy` 模板支持从 QMT_SIM 持久化订单 reference 恢复最大 seq。

验证：
- `F:/Program_Home/vnpy/python.exe -m pytest tests/test_qmt_sim_persistence.py vnpy_signal_strategy_plus/test/test_signal_journal.py -q`：16 passed，10 个第三方/utcnow deprecation warning。
- `F:/Program_Home/vnpy/python.exe -m py_compile ...` 覆盖本轮修改 Python 文件：通过。

风险/待办：
- 当前尚未跑全量 webtrader HTTP e2e 和真实 Redis/MySQL bridge 联调。
- `vnpy_signal_strategy_plus/test/redis_live_sim_setting.json`、`mysql_signal_setting.json`、`.env.example` 等已有本地改动未纳入本轮判断，需要提交时单独审查。
- 文档/README 中仍可能有旧 `stock_trade` 文案残留，P3 继续清理。

## 2026-05-17 - Redis dual-track v2 mirror and purge fix

Context:
- User ran run_signal_dual_track.py --mode v2 plus redis_to_mysql_bridge, then JoinQuant backtest.
- Diagnosis showed Redis stream and trade_signal_events for harvester_micro_cap_1 only reached remark=2026-05-08; no harvester_micro_cap_1_shadow rows existed.

Root causes:
- run_signal_dual_track.py still mirrored shadow rows from legacy stock_trade, while the bridge now writes only trade_signal_events.
- purge_test_strategy.py only targeted one strategy/account and had a corrupted persistence-dir key, so dual-track QMT/QMT_SIM_redis_shadow state and shadow v2 rows were not cleaned reliably.
- A no-signal tail such as 2026-05-09..2026-05-11 cannot be inferred from Redis order events; it needs explicit replay.settle_through or --settle-through.

Changes:
- Dual-track mirror now clones source trade_signal_events into target shadow stg with deterministic mirror signal_uid.
- Dynamic replay adapter supports final_settle_day; RedisLiveSimTestStrategy reads replay.settle_through/final_settle_date/end_date or CLI override.
- purge_test_strategy.py now cleans source + shadow v2 journal rows, QMT/QMT_SIM_redis_shadow sim DBs, Redis stream, and strategy_equity_journal rows after confirmation.

Validation:
- py_compile passed for run_signal_dual_track.py, replay_adapter.py, redis_live_sim_test_strategy.py and purge_test_strategy.py.
- Non-destructive purge parse check passed with all purge actions skipped; it resolved harvester_micro_cap_1 plus shadow and QMT/QMT_SIM_redis_shadow accounts.
- pytest passed: tests/test_qmt_sim_persistence.py, vnpy_signal_strategy_plus/test/test_signal_journal.py, vnpy_qmt_sim/test/test_sim_replay_controller.py (18 passed).

## 2026-05-17 - Redis replay no-signal tail settlement fix

Context:
- After running dual-track Redis/JQ replay, mlearnweb equity curves stopped at 2026-05-08 even though the JoinQuant backtest window ended at 2026-05-11.

Diagnosis:
- Redis stream harvester_micro_cap_1 had 112 messages and the latest remark was 2026-05-08 09:35:00.
- MySQL trade_signal_events for source and shadow both had max(remark)=2026-05-08 09:35:00; applications were consumed by QMT and QMT_SIM_redis_shadow.
- Local sim_QMT.db and sim_QMT_SIM_redis_shadow.db both had sim_meta.last_settle_date=2026-05-08.
- mlearnweb strategy_equity_snapshots had replay_settle and sim_live_settle rows only through 2026-05-08.
- Code bug: run_signal_dual_track.py populated replay.settle_through and RedisLiveSimTestStrategy parsed _final_settle_day, but CsvReplayTestStrategy.run_polling did not pass it into SignalJournalReplayAdapter.

Changes:
- CsvReplayTestStrategy.run_polling now forwards final_settle_day=getattr(self, "_final_settle_day", None) to SignalJournalReplayAdapter.
- Added a regression test proving run_polling forwards _final_settle_day to the adapter.
- run_signal_dual_track.py cleanup now removes strategy_signal_applications checkpoints for source + shadow strategies while keeping source trade_signal_events intact, so a clean sim rerun can replay existing source signals again.

Validation:
- py_compile passed for csv_replay_test_strategy.py, replay_adapter.py, redis_live_sim_test_strategy.py and test_signal_journal.py.
- py_compile also covered run_signal_dual_track.py after cleanup change.
- pytest passed: tests/test_qmt_sim_persistence.py, vnpy_signal_strategy_plus/test/test_signal_journal.py, vnpy_qmt_sim/test/test_sim_replay_controller.py (19 passed, with known third-party deprecation warnings).
- run_signal_dual_track.py --help passed and shows --settle-through.

Operational note:
- Redis/JQ sends order events only. The runner now provides the default no-signal tail boundary: if `--settle-through` is omitted, `run_signal_dual_track.py` resolves the latest completed trade day from the local qlib calendar and injects it into the strategy subclass.

## 2026-05-17 - Redis replay minimal correction after review

Context:
- User rejected broader source-label/UI masking and required the fix to target the real dual-track inconsistency with quantified evidence.
- Quant report already showed source/shadow v2 signals, orders, trades, positions and replay_settle are identical; inconsistent points came from `sim_live_settle` timer sampling during batch replay.

Changes:
- Kept `158d569 feat(signal): 引入 v2 信号 journal 与模拟账户恢复` as the committed v2 baseline.
- Removed the strategy-level runtime override / auto latest-trade-day inference from `RedisLiveSimTestStrategy`; it now only parses explicit config boundaries.
- `run_signal_dual_track.py` now owns the default boundary decision: no CLI `--settle-through` means settle through latest completed trade day from the local qlib calendar.
- `SignalJournalReplayAdapter` marks `replay_status=running` while processing historical v2 events and restores `idle` after idle/final finalize, so the global sim-live journal skips active batch replay.
- Added regression coverage for `_final_settle_day` forwarding and replay status transitions.
- Added `purge_signal_journal.py` / expanded `purge_test_strategy.py` for repeatable v2 source+shadow cleanup without touching legacy `stock_trade`.

Validation:
- `F:/Program_Home/vnpy/python.exe -m py_compile run_signal_dual_track.py vnpy_signal_strategy_plus/replay_adapter.py vnpy_signal_strategy_plus/strategies/csv_replay_test_strategy.py vnpy_signal_strategy_plus/strategies/redis_live_sim_test_strategy.py vnpy_signal_strategy_plus/scripts/purge_signal_journal.py vnpy_signal_strategy_plus/test/purge_test_strategy.py` passed.
- `F:/Program_Home/vnpy/python.exe -m pytest vnpy_signal_strategy_plus/test/test_signal_journal.py -q` passed: 6 passed, with only known third-party deprecation/cache warnings.
- `F:/Program_Home/vnpy/python.exe -m pytest vnpy_qmt_sim/test/test_sim_replay_controller.py tests/test_qmt_sim_persistence.py -q -p no:cacheprovider --basetemp ...` passed with elevated filesystem access: 15 passed, 3 third-party deprecation warnings. Non-elevated run failed before setup because pytest could not create temp directories under the sandbox.
- `purge_signal_journal.py --dry-run --stg harvester_micro_cap_1 --shadow-stg harvester_micro_cap_1_shadow` connected to MySQL with elevated network access and reported source/shadow each 112 `trade_signal_events` rows, both from 2026-01-07 09:35:00 to 2026-05-08 09:35:00, plus 111 ordered + 1 skipped application rows for each strategy. No rows were deleted.

Next:
- If the user wants a full live-path confirmation, rerun `run_signal_dual_track.py --mode v2 ...` with fresh MySQL/journal/sim cleanup and compare source/shadow `replay_settle` plus absence of mid-replay `sim_live_settle` rows.


## 2026-05-17 - SignalStrategyPlus 双轨启动器正式化与 v3 安全边界

背景/问题:
- 原入口 `run_signal_dual_track_demo.py` 已承担近实盘启动职责，文件名和说明仍带 demo，容易让 v3 真实 QMT 场景的安全边界不清晰。

本次结论:
- 正式入口改为 `run_signal_dual_track.py`。
- v1/single 为单 QMT_SIM 回放；v2 为 FakeQMT source + QMT_SIM shadow；v3 为真实 QMT source + QMT_SIM shadow。
- v3 source 腿默认不回放历史、不轮询/消费 MySQL 信号、不下单；只有显式传 `--allow-live-orders` 才会武装真实下单路径。
- v3 默认 `--live-signal-cutoff startup`，只消费启动后新信号；默认 `--cleanup-scope shadow`，不删除 source 策略消费 checkpoint。
- 脚本头部和 `--help` 已补充以 `harvester_micro_cap_1` / `harvester_micro_cap_1_shadow` 为例的典型命令。

验证:
- `run_signal_dual_track.py --help` 可正常展示中文模式语义和典型使用示例。
- 只读 `compile()` 校验通过：`run_signal_dual_track.py`、`mysql_signal_strategy.py`、`test_signal_dual_track_runner.py`、`test_signal_journal.py`。
- `F:/Program_Home/vnpy/python.exe -m pytest vnpy_signal_strategy_plus/test/test_signal_dual_track_runner.py vnpy_signal_strategy_plus/test/test_signal_journal.py -q -p no:cacheprovider --basetemp F:/Quant/code/qlib_strategy_dev/.tmp_pytest_vnpy_signal` 通过：10 passed，只有第三方/utcnow deprecation warnings。

风险与注意:
- 旧命令需从 `run_signal_dual_track_demo.py` 切换为 `run_signal_dual_track.py`。
- v3 加 `--allow-live-orders` 后会进入真实 QMT 下单路径，启动前必须确认账户、source stg 和 cutoff 策略。

## 2026-05-17 - VNPY_DATA_ROOT 去除硬编码回退

背景/问题:
- 服务器日志显示运行时静默回落到 `D:/vnpy_data`，导致 QMT_SIM 持久化初始化失败、merged 行情未命中并退化为合成 tick。
- 用户明确要求路径不对必须直接报错，不允许继续使用硬编码默认目录。

本次结论:
- `vnpy_common.data_paths.vnpy_data_root()` 不再提供任何硬编码数据根目录回退。
- `VNPY_DATA_ROOT` 缺失、为空、指向不存在目录或非目录时直接抛错。
- 配置模板和部署 helper 不再把 `D:/vnpy_data` 作为默认入口；部署必须显式填写数据根目录。

待验证:
- 需要在服务器设置真实存在的 `VNPY_DATA_ROOT` 后重新启动 `run_signal_dual_track.py` 或 `run_ml_headless.py`。
- 若服务器目录尚未创建，先创建 `<VNPY_DATA_ROOT>` 及其 `state/`、`snapshots/merged/` 等子目录，或执行显式 `-DataRoot` 的迁移脚本。

## 2026-05-17 - Signal dual-track 配置入口收敛

- 用户指出 `vnpy_signal_strategy_plus/test/redis_live_sim_setting.json` 仍是 `etf_rotation_basic` 等旧测试配置，不应作为正式双轨启动默认配置。
- 排查确认：普通单测未默认加载该文件；实际风险在 `run_signal_dual_track.py` 的 `--config` 默认值仍指向该 test JSON，`RedisLiveSimTestStrategy` 作为历史测试策略类也保留了 test 配置兜底。
- 已修改 `run_signal_dual_track.py`：默认配置改为 `SIGNAL_DUAL_TRACK_CONFIG` 或 `<VNPY_DATA_ROOT>/config/signal_dual_track.json`，缺失时直接报错；不再隐式读取 `vnpy_signal_strategy_plus/test/*.json`。
- 新增 `config/signal_dual_track.example.json`，默认策略名为 `harvester_micro_cap_1`，作为复制到数据根目录 config 下的模板。
