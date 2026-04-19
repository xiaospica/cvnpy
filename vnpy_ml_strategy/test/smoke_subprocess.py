"""Smoke test: vnpy_ml_strategy → QlibPredictor subprocess → 3 文件契约.

## 这个脚本测什么

端到端触发一次完整的推理 pipeline,验证:
  1. MLEngine 加载 QlibMLStrategy 类
  2. `init_strategy` 注册 daily cron job + 校验 bundle 目录
  3. `start_strategy` 进入 trading=True 状态
  4. `run_pipeline_now` → QlibPredictor.predict() → subprocess
     Python 3.11 + qlib bin 数据 → 三件套 (predictions.parquet /
     metrics.json / diagnostics.json) 原子落盘
  5. 主进程读三件套 → MetricsCache 填入 → 发 EVENT_ML_METRICS/_PREDICTION

## 和实盘的差异

实盘 (`run_ml_headless.py`):
  - 连真 gateway (QmtSimGateway / QmtGateway)
  - `is_trade_day(today)` 真调 tushare 日历
  - `run_daily_pipeline()` 用 `live_end=today`
  - scheduler 在每天 09:15 cron 触发

本 smoke:
  - **不连 gateway** (不 add_gateway + 不 connect)
  - **强制 `is_trade_day=True`** (今日若是周日, 实盘会短路, smoke 要强制跑)
  - **monkey-patch `run_daily_pipeline`** 把 `live_end` 固定到 2026-01-20
    (因为 bundle 训练 test 段止于 2026-01-23, 若用 today 会 status=empty)
  - **立即调 `run_pipeline_now`** 跑一次, 不等 09:15

## 前置

  - `F:/Program_Home/vnpy/python.exe` (vnpy 主进程 Python 3.13)
  - `E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe` (研究机 Python 3.11, 跑 qlib)
  - bundle: `F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab27...`
  - provider_uri: `F:/Quant/code/qlib_strategy_dev/factor_factory/qlib_data_bin`

## 不需要起别的服务

  - 不需要 webtrader uvicorn (没起 RPC server, 所以 REST 拿不到数据)
  - 不需要 mlearnweb (SQLite 不写, 事件只在进程内部传播)

## 运行

```
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_subprocess.py
```

预期 ~100s 完成, 打印 `DONE — all checks passed`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date as _d
from pathlib import Path

# sys.path 注入 — 脚本位于 vnpy_ml_strategy/test/, 上溯 2 层到 vnpy_strategy_dev 根
os.environ["VNPY_DOCK_BACKEND"] = "ads"
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]  # vnpy_strategy_dev root
# Put repo root on path first so local vnpy_ml_strategy / vnpy_webtrader wins
# over any site-packages copy.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "qlib_strategy_core"))
sys.path.insert(0, r"F:\Quant\code\qlib_strategy_dev")

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy_ml_strategy import MLStrategyApp, APP_NAME as ML_APP

BUNDLE = r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98"
OUT = r"D:/ml_output/smoke_subprocess"
STRATEGY_NAME = "smoke_subprocess"
TEST_LIVE_END = _d(2026, 1, 20)


def main() -> int:
    ev = EventEngine()
    main_engine = MainEngine(ev)
    main_engine.add_app(MLStrategyApp)
    eng = main_engine.get_engine(ML_APP)
    eng.init_engine()

    # Test-only override (see docstring).
    eng.is_trade_day = lambda d: True
    print(f"[smoke] registered: {eng.get_all_strategy_class_names()}", flush=True)

    # Event observation.
    events_seen = []
    for t in (
        f"eMlMetrics.{STRATEGY_NAME}",
        f"eMlPrediction.{STRATEGY_NAME}",
        f"eMlFailed.{STRATEGY_NAME}",
        f"eMlEmpty.{STRATEGY_NAME}",
        "eMlStrategy",
    ):
        ev.register(t, lambda e: events_seen.append(e.type))

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
    print(f"[smoke] add_strategy ok, inited={strat.inited} trading={strat.trading}", flush=True)

    # Test-only monkey patch: override run_daily_pipeline before init_strategy so
    # scheduler captures the patched callable. See module docstring for context.
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
    print(
        f"[smoke] (test) is_trade_day forced to True, run_daily_pipeline "
        f"patched to live_end={TEST_LIVE_END}",
        flush=True,
    )

    assert eng.init_strategy(STRATEGY_NAME), "init_strategy failed"
    assert eng.start_strategy(STRATEGY_NAME), "start_strategy failed"
    print(
        f"[smoke] init+start ok, inited={strat.inited} trading={strat.trading}",
        flush=True,
    )

    # Trigger pipeline immediately.
    print("[smoke] trigger pipeline (subprocess ~100s)...", flush=True)
    t0 = time.time()
    assert eng.run_pipeline_now(STRATEGY_NAME), "run_pipeline_now failed"

    # Wait for sentinel.
    out_day_dir = Path(OUT) / STRATEGY_NAME / TEST_LIVE_END.strftime("%Y%m%d")
    diag_path = out_day_dir / "diagnostics.json"
    deadline = time.time() + 300
    while not diag_path.exists() and time.time() < deadline:
        time.sleep(2)
    if not diag_path.exists():
        print("[smoke] FAIL: diagnostics.json not written within 300s")
        main_engine.close()
        return 1

    elapsed = time.time() - t0
    diag = json.loads(diag_path.read_text(encoding="utf-8"))
    metrics_path = out_day_dir / "metrics.json"
    m = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    print(
        f"[smoke] status={diag['status']} rows={diag['rows']} duration={elapsed:.1f}s",
        flush=True,
    )
    print(
        f"[smoke] metrics: ic={m.get('ic')} rank_ic={m.get('rank_ic')} "
        f"psi_mean={m.get('psi_mean')} n_pred={m.get('n_predictions')}",
        flush=True,
    )

    time.sleep(3)  # Wait for event pump.
    latest_cached = eng.get_latest_metrics(STRATEGY_NAME)
    print(f"[smoke] MetricsCache.latest populated: {latest_cached is not None}", flush=True)
    print(f"[smoke] events observed: {events_seen[:10]}", flush=True)

    # Clean shutdown.
    eng.stop_strategy(STRATEGY_NAME)
    main_engine.close()

    ok = diag["status"] == "ok" and latest_cached is not None
    print(f"[smoke] {'DONE — all checks passed' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
