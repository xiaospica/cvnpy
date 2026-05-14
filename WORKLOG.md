# WORKLOG

## 2026-05-12 RedisLiveSim v2 前端收益率为 0

背景：
- 用户使用 `run_signal_dual_track_demo.py --mode v2 --source-stg harvester_micro_cap_1 --shadow-stg harvester_micro_cap_1_shadow` 联合聚宽回测写 Redis 信号。
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
- 已修改 `run_signal_dual_track_demo.py`：启动时将 `REPLAY_HISTORY_DB` 固定到当前 sim `db_dir/replay_history.db`，避免 `.env.production` 的 `QS_DATA_ROOT` 把 WebTrader 指到错误位置。
- 已修改 `vnpy_qmt_sim/replay/controller.py`：当回放快照写入返回 False 时，将目标 DB 路径写入策略日志，便于后续定位。

验证：
- `F:/Program_Home/vnpy/python.exe -m py_compile run_signal_dual_track_demo.py vnpy_qmt_sim/replay/controller.py`
- mlearnweb DB 查询确认两个策略均有 `replay_settle=24` 行。
- `GET http://127.0.0.1:8100/api/live-trading/strategies/local/SignalStrategyPlus/{name}/performance-summary` 返回非零累计收益。

注意：
- 当前运行中的 v2 进程不会自动加载这次脚本修改；下次重启 `run_signal_dual_track_demo.py` 后生效。
- 如果使用默认清理启动，会删除本次 demo 相关 sim DB / replay_history 快照，需要重新跑聚宽回测或使用 `--no-cleanup` 保留已有状态。

## 2026-05-12 聚宽回测 vs 本地 v2 复现对账

背景：
- 用户运行 bridge、`run_signal_dual_track_demo.py --mode v2 --source-stg harvester_micro_cap_1 --shadow-stg harvester_micro_cap_1_shadow`，并在聚宽上开启回测写 Redis 信号。
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
