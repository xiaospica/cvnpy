"""Smoke test: 全 vnpy 进程 + WebTrader RPC + 自动派生 webtrader uvicorn.

## 这个脚本测什么

一个命令启起来:MLEngine + WebTrader RPC + webtrader REST uvicorn :8001
全部就绪. 用户单终端跑,脚本内部 subprocess 派生 uvicorn 子进程,
退出时清理.

验证点:
  - MLStrategyAdapter → MLEngine.get_latest_metrics / history / prediction_summary / health 的 RPC 调用
  - `/api/v1/ml/*` 5 端点的 JWT + 返回 schema
  - `_load_latest_topk` 从 disk selections.parquet 读数据

## 和实盘的差异

实盘 (`run_ml_headless.py`):
  - 连真 gateway,真 `is_trade_day`,每日 09:15 cron 触发
  - `live_end=today`
  - webtrader uvicorn 是**独立进程**(生产侧用 Qt widget 或 systemd 启,跟 trader
    解耦,trader 下线 REST 仍活)

本 smoke:
  - **不连 gateway**,强制 `is_trade_day=True`
  - **monkey-patch** `live_end=2026-01-10` (bundle 数据不覆盖 today)
  - 为了一条命令起全链路,webtrader uvicorn 作为**子进程**派生,脚本退出时一起清理.
    生产保真度略降(trader 和 uvicorn 实际应解耦), 但对测试足够.

## 前置

  - 端口空闲: 2014 / 4102 / 8001
  - 同 `smoke_subprocess.py` 的 bundle / provider_uri

## 运行(单终端)

```
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_engine_rpc.py
```

预期输出:
  [smoke] webtrader RPC server on tcp://127.0.0.1:2014 / 4102
  [smoke] registered: ['QlibMLStrategy']
  [smoke] strategy inited+started, inited=True trading=True
  [smoke] TRIGGER_PIPELINE_ON_STARTUP=False → skipping subprocess.
  [smoke] spawned webtrader uvicorn pid=... on :8001
  [smoke] READY — trading + webtrader REST 全部就绪. Ctrl+C 退出.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date as _d
from pathlib import Path

os.environ["VNPY_DOCK_BACKEND"] = "ads"
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]  # vnpy_strategy_dev root
# CRITICAL: repo root first, or Python will pick up the OLD site-packages
# vnpy_webtrader==1.1.0 (which lacks list_strategies / /ml/* routes) and the
# uvicorn side will get "KeyError: 'list_strategies'" on /api/v1/strategy.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "qlib_strategy_core"))
sys.path.insert(0, r"F:\Quant\code\qlib_strategy_dev")

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy_ml_strategy import MLStrategyApp, APP_NAME as ML_APP
from vnpy_webtrader import WebTraderApp
from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME

BUNDLE = r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98"
# OUT 指向 backfill 目录 —— 让 adapter 的 _load_latest_topk 能读到 backfill 写的
# selections.parquet (prod 语义: 每日 persist_selections 写的地方)
OUT = r"D:/ml_output/phase27_backfill"
STRATEGY_NAME = "phase27_test"
TEST_LIVE_END = _d(2026, 1, 10)

# 是否在启动后立即跑一次 subprocess 推理.
#
# True  → 验证全 subprocess 链路 (~100s). 会覆盖 MetricsCache.latest, 注意
#         ml_snapshot_loop 下次 tick 会把这个 "2026-01-10" 的结果 UPSERT
#         到 SQLite, 可能干扰已 backfill 的历史数据可视化.
# False → 只注册 + init + start 策略, MetricsCache 保持空. 适合 "我只想让
#         前端 UI 看到策略存在并展示历史 SQLite 数据" 的场景.
TRIGGER_PIPELINE_ON_STARTUP = False


def main() -> int:
    ev = EventEngine()
    main_engine = MainEngine(ev)
    main_engine.add_app(MLStrategyApp)
    main_engine.add_app(WebTraderApp)
    eng = main_engine.get_engine(ML_APP)
    eng.init_engine()

    # WebEngine RPC server (Qt widget starts this via button in GUI mode).
    web_engine = main_engine.get_engine(WEB_APP_NAME)
    web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
    print("[smoke] webtrader RPC server on tcp://127.0.0.1:2014 / 4102", flush=True)

    eng.is_trade_day = lambda d: True
    print(f"[smoke] registered: {eng.get_all_strategy_class_names()}", flush=True)

    strat = eng.add_strategy("QlibMLStrategy", STRATEGY_NAME, {
        "bundle_dir": BUNDLE,
        "inference_python": r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe",
        "provider_uri": r"F:/Quant/code/qlib_strategy_dev/factor_factory/qlib_data_bin",
        "output_root": OUT,
        "gateway": "QMT_SIM",
        "trigger_time": "09:15",
        "topk": 7,
        "cash_per_order": 100000,
        "lookback_days": 60,
        "subprocess_timeout_s": 300,
        "enable_trading": False,
    })

    def _test_pipeline() -> None:
        strat.last_run_date = str(TEST_LIVE_END)
        strat.last_error = ""
        result = eng.run_inference(
            bundle_dir=strat.bundle_dir,
            live_end=TEST_LIVE_END,
            lookback_days=strat.lookback_days,
            strategy_name=strat.strategy_name,
            inference_python=strat.inference_python,
            output_root=strat.output_root,
            provider_uri=strat.provider_uri,
            baseline_path=strat.baseline_path or None,
            timeout_s=strat.subprocess_timeout_s,
        )
        diag = result["diagnostics"]
        metrics = result.get("metrics", {})
        strat.last_status = diag.get("status", "")
        strat.last_duration_ms = diag.get("duration_ms", 0)
        strat.last_model_run_id = diag.get("model_run_id", "")
        strat.last_n_pred = diag.get("rows", 0)
        strat.last_ic = metrics.get("ic") or float("nan")
        strat.last_psi_mean = metrics.get("psi_mean") or float("nan")
        strat._publish_metrics(metrics)
        if diag.get("status") == "empty":
            strat._emit_empty()
        elif diag.get("status") == "failed":
            strat._emit_failed(diag.get("error_message", ""))
        else:
            pred_df = result.get("pred_df")
            if pred_df is not None and not pred_df.empty:
                selected = strat.select_topk(pred_df)
                strat._emit_prediction(selected, metrics)

    strat.run_daily_pipeline = _test_pipeline

    assert eng.init_strategy(STRATEGY_NAME), "init_strategy failed"
    assert eng.start_strategy(STRATEGY_NAME), "start_strategy failed"
    print(
        f"[smoke] strategy inited+started, inited={strat.inited} trading={strat.trading}",
        flush=True,
    )

    if TRIGGER_PIPELINE_ON_STARTUP:
        print("[smoke] TRIGGER_PIPELINE_ON_STARTUP=True → running subprocess (~100s)...", flush=True)
        t0 = time.time()
        assert eng.run_pipeline_now(STRATEGY_NAME), "run_pipeline_now failed"

        out_day_dir = Path(OUT) / STRATEGY_NAME / TEST_LIVE_END.strftime("%Y%m%d")
        diag_path = out_day_dir / "diagnostics.json"
        deadline = time.time() + 300
        while not diag_path.exists() and time.time() < deadline:
            time.sleep(2)

        elapsed = time.time() - t0
        print(
            f"[smoke] pipeline done, elapsed={elapsed:.1f}s, diag exists={diag_path.exists()}",
            flush=True,
        )

        if diag_path.exists():
            diag = json.loads(diag_path.read_text(encoding="utf-8"))
            print(f"[smoke] status={diag['status']} rows={diag['rows']}", flush=True)

        time.sleep(3)  # event pump
        latest = eng.get_latest_metrics(STRATEGY_NAME)
        print(f"[smoke] MetricsCache.latest: {latest is not None}", flush=True)
        if latest:
            print(
                f"[smoke] n_predictions={latest.get('n_predictions')} "
                f"psi_mean={latest.get('psi_mean')} ic={latest.get('ic')}",
                flush=True,
            )
    else:
        print(
            "[smoke] TRIGGER_PIPELINE_ON_STARTUP=False → skipping subprocess. "
            "UI will read history from mlearnweb SQLite (backfill).",
            flush=True,
        )

    # Spawn webtrader REST uvicorn as a child process so the user only needs
    # one terminal. When this process exits (Ctrl+C or crash), we tear the
    # uvicorn child down too.
    uvicorn_proc = subprocess.Popen(
        [
            sys.executable, "-u", "-m", "uvicorn",
            "vnpy_webtrader.web:app",
            "--host", "127.0.0.1", "--port", "8001",
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print(f"[smoke] spawned webtrader uvicorn pid={uvicorn_proc.pid} on :8001", flush=True)
    # Give uvicorn a few seconds to bind + connect RPC.
    time.sleep(3)
    if uvicorn_proc.poll() is not None:
        print(f"[smoke] FAIL: webtrader uvicorn exited early (rc={uvicorn_proc.returncode})", flush=True)
        main_engine.close()
        return 2

    print(
        "[smoke] READY — trading + webtrader REST 全部就绪 on :2014 / :4102 / :8001",
        flush=True,
    )
    print("[smoke] Ctrl+C to exit (will also tear down uvicorn child).", flush=True)

    stop = {"v": False}

    def _sigint(_s, _f):
        stop["v"] = True

    signal.signal(signal.SIGINT, _sigint)
    while not stop["v"]:
        time.sleep(1)
        if uvicorn_proc.poll() is not None:
            print(
                f"[smoke] uvicorn child exited unexpectedly (rc={uvicorn_proc.returncode}). "
                "Shutting down.",
                flush=True,
            )
            stop["v"] = True

    print("[smoke] shutting down uvicorn child...", flush=True)
    try:
        uvicorn_proc.terminate()
        uvicorn_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        uvicorn_proc.kill()
    print("[smoke] shutting down trader...", flush=True)
    eng.stop_strategy(STRATEGY_NAME)
    main_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
