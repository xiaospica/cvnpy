# vnpy_ml_strategy 集成 smoke tests

这些脚本**独立于实盘**,用来验证 vnpy_ml_strategy app 的关键链路。生产启动由 `run_ml_headless.py` 或 Qt UI (`run_sim.py`) 负责,与本目录无关。

## 测试矩阵

| 脚本 | 测试链路 | 需要的前置服务 | 和实盘的差异 | 能/不能测 |
|---|---|---|---|---|
| `smoke_subprocess.py` | Python 3.13 主进程 → QlibPredictor → subprocess (Python 3.11) → 3 文件契约 | 无 (只需 Python 3.11 + qlib bin 数据 + bundle) | 不起 MLEngine / MainEngine / gateway;直接实例化 MLEngine 挂空 EventEngine | ✅ subprocess 启动 / 3 文件落盘 / IC 计算正确 / 超时 / 失败语义<br>❌ 不测 gateway 连接 / 下单 / 事件传播到 webtrader |
| `smoke_engine_rpc.py` | MLEngine 完整 + WebTrader RPC + 派生 webtrader uvicorn (单终端) | 无 | 同 smoke_subprocess;额外把 webtrader uvicorn 当子进程派生 (生产侧应为独立进程,本脚本为测试便利合并) | ✅ 全 pipeline + RPC + REST 单条命令起<br>❌ 不测自动定时触发 (09:15 cron 被绕过) |

## 运行时序

### 1. `smoke_subprocess.py` — 最小链路

```bash
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_subprocess.py
```

**前置**:
- `E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe` 存在 (研究机 Python)
- `F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98` 有完整 bundle (params.pkl + task.json + manifest.json + baseline.parquet)
- `F:/Quant/code/qlib_strategy_dev/factor_factory/qlib_data_bin` 是 qlib provider 目录

**预期输出**:
```
[smoke] registered: ['QlibMLStrategy']
[smoke] (test) is_trade_day forced to True, run_daily_pipeline patched to live_end=2026-01-20
[smoke] init_strategy → True
[smoke] start_strategy → True
[smoke] trigger pipeline (subprocess ~100s)...
[smoke] status=ok rows=12544 duration=~95s
[smoke] metrics: ic=... psi_mean=... n_pred=12544
[smoke] DONE
```

**验证了什么**:
- subprocess 调用 / PYTHONPATH 注入 / qlib dataset 构建
- 3 文件原子写 (predictions.parquet → metrics.json → diagnostics.json)
- `compute_ic` / `compute_rank_ic` / PSI / 直方图 / feat_missing 在真实数据上 OK
- `MetricsCache` 被事件回调填充
- QlibPredictor 的超时 / 失败语义

**没测**:
- gateway 连接
- 下单 (selections 会写,但 `generate_orders` 在 `enable_trading=False` 时跳过)
- 自动定时触发 (scheduler.run_job_now 手动调,不走真实 09:15 cron)
- webtrader REST (没起 RPC 服务器,所以没 uvicorn 能连)

### 2. `smoke_engine_rpc.py` — 全链路(单终端, 自带 uvicorn 子进程)

```bash
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_engine_rpc.py
```

脚本内部派生 webtrader uvicorn 子进程,Ctrl+C 时一并清理。

**前置**:
- 同 `smoke_subprocess.py`
- 端口 2014 / 4102 / 8001 空闲

**预期**:
```
[smoke] webtrader RPC server on tcp://127.0.0.1:2014 / 4102
[smoke] registered: ['QlibMLStrategy']
[smoke] strategy inited+started, inited=True trading=True
[smoke] TRIGGER_PIPELINE_ON_STARTUP=False → skipping subprocess.
[smoke] spawned webtrader uvicorn pid=... on :8001
[smoke] READY — trading + webtrader REST 全部就绪 on :2014 / :4102 / :8001
[smoke] Ctrl+C to exit (will also tear down uvicorn child).
```

另开终端 curl 验证:
```bash
curl -X POST -d "username=vnpy&password=vnpy" http://127.0.0.1:8001/api/v1/token
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8001/api/v1/ml/health
```

**和实盘 (`run_ml_headless.py`) 的差异**:
- 实盘 `is_trade_day(today)` 真调 tushare 日历,周末/节假日会短路;smoke 里强制 `True`
- 实盘 `run_daily_pipeline()` 用 `live_end=today`;smoke 里 monkey patch 成 `live_end=2026-01-10`(因为测试期 bundle 数据不覆盖 today)
- 实盘调度器 cron 触发,smoke 立即触发一次 `run_pipeline_now` 就挂等

**不测**:
- 09:15 真实 cron 触发
- 跨日重复运行(每次跑就是一次 live_end,不累积)

## 启动完整 UI 链路 (ML 监控面板)

smoke 脚本只到 vnpy 节点级。要看 mlearnweb UI:

```bash
# 终端 D (mlearnweb 研究侧)
cd /f/Quant/code/qlib_strategy_dev/mlearnweb/backend
E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe -m uvicorn app.main:app --port 8000 --reload

# 终端 E (mlearnweb 实盘侧, 带 ML_LIVE_OUTPUT_ROOT 环境变量启用 backtest-vs-live)
cd /f/Quant/code/qlib_strategy_dev/mlearnweb/backend
ML_LIVE_OUTPUT_ROOT=D:/ml_output/phase27_backfill \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe -m uvicorn app.live_main:app --port 8100

# 终端 F (前端)
cd /f/Quant/code/qlib_strategy_dev/mlearnweb/frontend
npm run dev
```

UI: `http://localhost:5173/live-trading/local/MlStrategy/phase27_test`

当终端 A+B 活着时,UI 会从 vnpy 节点实时拿运行状态、持仓、参数;终端 A+B 不活时,UI 优雅降级到 SQLite 历史数据(仅显示 `MlMonitorPanel`,上方贴提示条)。

## 纯回填验证(不起 vnpy,只用 SQLite)

参见 `mlearnweb/backend/scripts/phase27_backfill_inference.py` + 配套
`phase27_backfill_topk.py` —— 用 CLI 串跑 N 个历史交易日的 subprocess,直接 UPSERT
SQLite,绕过所有 vnpy 进程。跑完只需启终端 D+E+F 就能看 UI。细节看该脚本开头注释。

## 相关调试工具(不在本目录,但相关)

- `vendor/qlib_strategy_core/scripts/probe_label_dataset.py` — 调试 pred/label 对齐
- `vendor/qlib_strategy_core/scripts/probe_compute_ic.py` — 调试 compute_ic 返 nan
