"""Smoke full pipeline — 一条命令模拟生产一天全流程.

## 测什么

比 `smoke_engine_rpc.py` 多两层:

  1. **启动期调 DailyIngestPipeline** 拉真实今日 tushare 数据 + 过滤 parquet
     + qlib bin 增量 dump (Phase 4 P4.1 的日更管道真实跑一次)
  2. **派生 mlearnweb app.live_main uvicorn 子进程** :8100 让 ml_snapshot_loop
     每 60s 轮询 webtrader /api/v1/ml/* → UPSERT SQLite

然后立即触发 MLEngine.run_pipeline_now 跑 subprocess 推理 (不 monkey-patch),
真正 live_end=today 的推理. 等一个 ml_snapshot_loop tick (60s+10s) 后做 12 条
端到端断言.

完整流程 7 阶段:
  Phase 0 前置: 读 api.json token + 校验聚宽 CSV / qlib bin 历史
  Phase 1 起 vnpy 全栈 + webtrader uvicorn + mlearnweb live_main uvicorn
  Phase 2 拉真实今日数据 (DailyIngestPipeline.ingest_today)
  Phase 3 add_strategy("QlibMLStrategy", ..., trigger_time="21:00")
  Phase 4 run_pipeline_now (~100s subprocess)
  Phase 5 等 ml_snapshot_loop tick
  Phase 6 12 条验证断言
  Phase 7 常驻等 Ctrl+C, 清理 2 个 uvicorn 子进程

## 和实盘差异

- 实盘 run_ml_headless.py: 20:00 cron 拉数 + 21:00 cron 推理, 不常驻也不派生 uvicorn
- 本脚本: 全立即触发, 派生 2 个 uvicorn 子进程做端到端

## 前置

- `F:\\Quant\\vnpy\\vnpy_strategy_dev\\api.json` 含 tushare token (格式 {"token":"xxx"})
- env 变量 ML_JQ_INDEX_CSV_PATHS (聚宽 CSV 路径 JSON)
- 端口 2014/4102/8001/8100 空闲
- qlib bin 已有 >= 70 交易日历史
- 当日是交易日 (否则 DailyIngestPipeline 会 skipped=True, 继续走推理但 live_end 非今日)
- `D:\\ml_output\\smoke_full_pipeline\\` 可写

## 运行

```
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_full_pipeline.py
```

预期输出末尾:
    [smoke_full] READY — 生产 1 日流程演练完成, 所有断言通过.
    [smoke_full] Ctrl+C to exit.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path


# --- sys.path 注入 ------------------------------------------------------

os.environ["VNPY_DOCK_BACKEND"] = "ads"
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2]  # vnpy_strategy_dev
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "qlib_strategy_core"))
sys.path.insert(0, r"F:\Quant\code\qlib_strategy_dev")


# --- 配置 ---------------------------------------------------------------

TRIGGER_INGEST_ON_STARTUP: bool = True   # False → 只跑 vnpy 不拉数 (快速 iter)
TRIGGER_PIPELINE_ON_STARTUP: bool = True # False → 只拉数不推理
SPAWN_MLEARNWEB: bool = True              # False → 不启 mlearnweb live_main
# LIVE_DATE 默认 today, 但若非交易日会自动往前找最近交易日 (见 _resolve_live_date).
# 也可通过 env LIVE_DATE=YYYY-MM-DD 手动指定.
LIVE_DATE: date = date.today()

BUNDLE_DIR = r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98"
OUT_ROOT = r"D:/ml_output/smoke_full_pipeline"
STRATEGY_NAME = "phase27_test"

# Phase 4 v2: QS_DATA_ROOT 驱动所有路径, 实盘/训练解耦
QS_DATA_ROOT = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")

PROVIDER_URI = f"{QS_DATA_ROOT}/qlib_data_bin"
MERGED_PARQUET = f"{QS_DATA_ROOT}/stock_data/daily_merged_all_new.parquet"
FILTERED_PARQUET = f"{QS_DATA_ROOT}/csi300_custom_filtered.parquet"
BY_STOCK_DIR = f"{QS_DATA_ROOT}/stock_data/by_stock"
SNAPSHOT_DIR = f"{QS_DATA_ROOT}/snapshots"
JQ_INDEX_CSV_PATHS_JSON = os.getenv(
    "ML_JQ_INDEX_CSV_PATHS",
    json.dumps({"csi300": f"{QS_DATA_ROOT}/jq_index/hs300_*.csv"}),
)

MLEARNWEB_BACKEND = r"F:/Quant/code/qlib_strategy_dev/mlearnweb/backend"
MLEARNWEB_DB = Path(MLEARNWEB_BACKEND) / "mlearnweb.db"
PY311 = r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe"


# --- 工具 ---------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[smoke_full] {msg}", flush=True)


def _load_tushare_token() -> str:
    api_json = ROOT / "api.json"
    if not api_json.exists():
        raise FileNotFoundError(f"api.json 不存在: {api_json}")
    data = json.loads(api_json.read_text(encoding="utf-8"))
    token = data.get("token") or data.get("password") or data.get("tushare_token")
    if not token:
        raise RuntimeError(f"api.json 里找不到 tushare token: {data.keys()}")
    return token


def _resolve_live_date(downloader) -> date:
    """返回最近交易日: 若 env LIVE_DATE 指定则尊重; 否则 today 非交易日自动回退."""
    from datetime import timedelta
    env_val = os.getenv("LIVE_DATE")
    if env_val:
        try:
            return datetime.strptime(env_val, "%Y-%m-%d").date()
        except ValueError:
            _log(f"WARN: env LIVE_DATE={env_val} 格式非法, 忽略")

    # today 往前找 10 天里最近的交易日
    candidate = date.today()
    for _ in range(10):
        try:
            is_td = bool(downloader.is_trade_date(candidate.strftime("%Y%m%d")))
        except Exception:
            is_td = True  # 失败保守当交易日
        if is_td:
            return candidate
        candidate = candidate - timedelta(days=1)
    return candidate  # fallback (理论上 10 天内必有交易日)


def _setup_ingest_env(token: str) -> None:
    """把 DailyIngestPipeline 需要的 env 变量塞进 os.environ."""
    os.environ["TUSHARE_TOKEN"] = token
    os.environ["ML_DAILY_INGEST_ENABLED"] = "1"
    # QS_DATA_ROOT 是单一入口, 其他 ML_* 路径从它派生 (tushare_datafeed 内部)
    os.environ["QS_DATA_ROOT"] = QS_DATA_ROOT
    # 若需要细粒度覆盖(测试用), 显式设 ML_*:
    os.environ["ML_MERGED_PARQUET_PATH"] = MERGED_PARQUET
    os.environ["ML_FILTERED_PARQUET_PATH"] = FILTERED_PARQUET
    os.environ["ML_BY_STOCK_CSV_DIR"] = BY_STOCK_DIR
    os.environ["ML_QLIB_DIR"] = PROVIDER_URI
    os.environ["ML_SNAPSHOT_DIR"] = SNAPSHOT_DIR
    os.environ["ML_JQ_INDEX_CSV_PATHS"] = JQ_INDEX_CSV_PATHS_JSON
    # vnpy tushare datafeed 需要
    os.environ.setdefault("VNPY_DATAFEED_USERNAME", "tushare")
    os.environ.setdefault("VNPY_DATAFEED_PASSWORD", token)


# --- 主流程 -------------------------------------------------------------

def main() -> int:
    _log(f"=== Phase 0 — 前置 ===")
    token = _load_tushare_token()
    _setup_ingest_env(token)

    # --- Phase 1: vnpy 全栈 ---
    _log("=== Phase 1 — vnpy 全栈 ===")
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.setting import SETTINGS
    # 让 vnpy_tushare_pro 的 SETTINGS 也认这个 password
    SETTINGS["datafeed.username"] = "tushare"
    SETTINGS["datafeed.password"] = token

    from vnpy_ml_strategy import MLStrategyApp, APP_NAME as ML_APP
    from vnpy_tushare_pro import TushareProApp
    from vnpy_tushare_pro.engine import (
        APP_NAME as TUSHARE_APP,
        EVENT_DAILY_INGEST_OK,
        EVENT_DAILY_INGEST_FAILED,
    )
    from vnpy_webtrader import WebTraderApp
    from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME

    ev = EventEngine()
    main_engine = MainEngine(ev)
    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)
    main_engine.add_app(WebTraderApp)

    tushare_engine = main_engine.get_engine(TUSHARE_APP)
    tushare_engine.init_engine()
    _log(f"TushareProEngine inited, daily_ingest_pipeline={tushare_engine._get_tushare_datafeed().daily_ingest_pipeline is not None}")

    # 现在 tushare engine 已就绪, 确定 LIVE_DATE (非交易日自动回退)
    downloader = tushare_engine._get_tushare_datafeed().downloader
    live_date = _resolve_live_date(downloader)
    live_date_str = live_date.strftime("%Y%m%d")
    live_date_iso = live_date.strftime("%Y-%m-%d")
    if live_date != date.today():
        _log(f"WARN: today={date.today()} 非交易日, 回退 LIVE_DATE={live_date}")
        # template.run_daily_pipeline 用 date.today() 决定 live_end + trade_day 短路.
        # 非交易日 smoke 必须 monkey-patch, 否则 pipeline 直接发 heartbeat 不跑推理.
        import vnpy_ml_strategy.template as _tpl_mod
        _orig_date = _tpl_mod.date

        class _SmokeDate(_orig_date):
            @classmethod
            def today(cls):
                return live_date
        _tpl_mod.date = _SmokeDate
        _log(f"monkey-patched vnpy_ml_strategy.template.date.today() -> {live_date}")
    else:
        _log(f"LIVE_DATE={live_date}")

    ml_engine = main_engine.get_engine(ML_APP)
    ml_engine.init_engine()
    _log(f"MLEngine registered: {ml_engine.get_all_strategy_class_names()}")

    web_engine = main_engine.get_engine(WEB_APP_NAME)
    web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
    _log("webtrader RPC on :2014 / :4102")

    # 派生 webtrader uvicorn
    webtrader_uv = subprocess.Popen(
        [
            sys.executable, "-u", "-m", "uvicorn",
            "vnpy_webtrader.web:app", "--host", "127.0.0.1", "--port", "8001",
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    _log(f"spawned webtrader uvicorn pid={webtrader_uv.pid} on :8001")

    # 派生 mlearnweb live_main uvicorn
    mlearnweb_uv = None
    if SPAWN_MLEARNWEB:
        mlearnweb_uv = subprocess.Popen(
            [
                PY311, "-u", "-m", "uvicorn",
                "app.live_main:app", "--host", "127.0.0.1", "--port", "8100",
            ],
            cwd=MLEARNWEB_BACKEND,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "ML_LIVE_OUTPUT_ROOT": OUT_ROOT,
                "VNPY_SNAPSHOT_RETENTION_DAYS": "365",
            },
        )
        _log(f"spawned mlearnweb live_main pid={mlearnweb_uv.pid} on :8100")

    time.sleep(4)  # 给 uvicorn 启动时间

    if webtrader_uv.poll() is not None:
        _log(f"FAIL: webtrader uvicorn died early (rc={webtrader_uv.returncode})")
        return 2

    # --- Phase 2: 拉真实数据 ---
    if TRIGGER_INGEST_ON_STARTUP:
        _log("=== Phase 2 — DailyIngestPipeline.ingest_today ===")
        t0 = time.time()
        result = tushare_engine.run_daily_ingest_now(live_date_str)
        if result is None:
            _log("FAIL: run_daily_ingest_now 返回 None (pipeline 未配置)")
            _teardown(main_engine, webtrader_uv, mlearnweb_uv)
            return 3
        if result.get("skipped"):
            _log(f"WARN: {live_date_str} 非交易日 skipped, 推理将用 live_end=today 跑但可能 status=empty")
        else:
            _log(f"ingest OK stages={result['stages_done']} merged_rows={result['merged_rows']} filtered_today_rows={result.get('filtered_today_rows', result.get('filtered_rows'))} elapsed={time.time()-t0:.1f}s")
    else:
        _log("Phase 2 skipped (TRIGGER_INGEST_ON_STARTUP=False)")

    # --- Phase 3: add + init + start 策略 ---
    _log("=== Phase 3 — add_strategy ===")
    strat = ml_engine.add_strategy("QlibMLStrategy", STRATEGY_NAME, {
        "bundle_dir": BUNDLE_DIR,
        "inference_python": PY311,
        "provider_uri": PROVIDER_URI,
        "output_root": OUT_ROOT,
        "gateway": "QMT_SIM",
        "trigger_time": "21:00",
        "topk": 7,
        "cash_per_order": 100000,
        "lookback_days": 60,
        "subprocess_timeout_s": 300,
        "enable_trading": False,
    })
    assert ml_engine.init_strategy(STRATEGY_NAME)
    assert ml_engine.start_strategy(STRATEGY_NAME)
    _log(f"strategy inited+started, inited={strat.inited} trading={strat.trading}")

    # --- Phase 4: run_pipeline_now ---
    if TRIGGER_PIPELINE_ON_STARTUP:
        _log("=== Phase 4 — run_pipeline_now (~100s) ===")
        t0 = time.time()
        assert ml_engine.run_pipeline_now(STRATEGY_NAME)
        out_day_dir = Path(OUT_ROOT) / STRATEGY_NAME / live_date_str
        diag_path = out_day_dir / "diagnostics.json"
        deadline = time.time() + 300
        while not diag_path.exists() and time.time() < deadline:
            time.sleep(2)
        _log(f"pipeline done elapsed={time.time()-t0:.1f}s")
    else:
        _log("Phase 4 skipped (TRIGGER_PIPELINE_ON_STARTUP=False)")

    # --- Phase 5: 等 ml_snapshot_loop tick ---
    if SPAWN_MLEARNWEB and TRIGGER_PIPELINE_ON_STARTUP:
        _log("=== Phase 5 — 等 ml_snapshot_loop tick (70s) ===")
        time.sleep(70)

    # --- Phase 6: 验证清单 ---
    _log("=== Phase 6 — 验证 ===")
    errors = _run_assertions(live_date_str, live_date_iso)
    if errors:
        _log(f"FAIL: {len(errors)} 条断言失败:")
        for e in errors:
            _log(f"  - {e}")
        _teardown(main_engine, webtrader_uv, mlearnweb_uv)
        return 4

    _log("所有断言通过 ✓")

    # --- Phase 7: 常驻 ---
    _log("=== Phase 7 — READY ===")
    _log("生产 1 日流程演练完成, 所有断言通过.")
    _log("trading + webtrader REST + mlearnweb SQLite 全部就绪 on :2014 / :4102 / :8001 / :8100")
    _log("Ctrl+C to exit (will tear down 2 uvicorn children).")

    stop = {"v": False}

    def _sigint(_s, _f):
        stop["v"] = True

    signal.signal(signal.SIGINT, _sigint)
    while not stop["v"]:
        time.sleep(1)
        if webtrader_uv.poll() is not None:
            _log(f"webtrader uvicorn 自退 (rc={webtrader_uv.returncode})")
            stop["v"] = True
        if mlearnweb_uv is not None and mlearnweb_uv.poll() is not None:
            _log(f"mlearnweb uvicorn 自退 (rc={mlearnweb_uv.returncode})")
            stop["v"] = True

    _teardown(main_engine, webtrader_uv, mlearnweb_uv)
    return 0


def _run_assertions(live_date_str: str, live_date_iso: str) -> list[str]:
    """12 条断言, 返回失败列表 (空列表代表全通)."""
    import pandas as pd
    errors: list[str] = []

    def _check(cond: bool, msg: str):
        if not cond:
            errors.append(msg)

    # [a] merged_parquet trade_date.max() >= LIVE_DATE
    try:
        merged = pd.read_parquet(MERGED_PARQUET, columns=["trade_date"])
        tmax = pd.to_datetime(merged["trade_date"]).max()
        _check(tmax >= pd.Timestamp(live_date_str), f"[a] merged tmax={tmax.date()} < {live_date_str}")
    except Exception as e:
        errors.append(f"[a] merged parquet read 失败: {e}")

    # [b] merged snapshot
    snap_dir = Path(SNAPSHOT_DIR) / "merged"
    snaps = list(snap_dir.glob(f"*{live_date_str}*.parquet")) if snap_dir.exists() else []
    _check(len(snaps) > 0, f"[b] merged snapshot 缺失 in {snap_dir}")

    # [c] filtered_parquet 含 LIVE_DATE
    try:
        filtered = pd.read_parquet(FILTERED_PARQUET)
        today_rows = filtered[pd.to_datetime(filtered["trade_date"]) == pd.Timestamp(live_date_str)]
        _check(200 <= len(today_rows) <= 310, f"[c] filtered {live_date_str} rows={len(today_rows)} (期望 200-310)")
    except Exception as e:
        errors.append(f"[c] filtered parquet read 失败: {e}")

    # [d] filtered snapshot
    fsnap = Path(SNAPSHOT_DIR) / "filtered" / f"csi300_filtered_{live_date_str}.parquet"
    _check(fsnap.exists(), f"[d] filtered snapshot 缺失: {fsnap}")

    # [e] calendars/day.txt
    cal_path = Path(PROVIDER_URI) / "calendars" / "day.txt"
    if cal_path.exists():
        lines = cal_path.read_text(encoding="utf-8").splitlines()
        _check(lines[-1] == live_date_iso, f"[e] calendars last={lines[-1]} != {live_date_iso}")
    else:
        errors.append(f"[e] calendars/day.txt 不存在: {cal_path}")

    # [f] diagnostics.json status=ok
    out_day = Path(OUT_ROOT) / STRATEGY_NAME / live_date_str
    diag_p = out_day / "diagnostics.json"
    if diag_p.exists():
        diag = json.loads(diag_p.read_text(encoding="utf-8"))
        _check(diag.get("status") == "ok", f"[f] diagnostics status={diag.get('status')}")
        _check(diag.get("rows", 0) > 0, f"[f] diagnostics rows={diag.get('rows')}")
    else:
        errors.append(f"[f] diagnostics.json 缺失: {diag_p}")

    # [g] metrics.json
    m_p = out_day / "metrics.json"
    if m_p.exists():
        m = json.loads(m_p.read_text(encoding="utf-8"))
        _check(m.get("n_predictions", 0) > 0, f"[g] metrics n_predictions={m.get('n_predictions')}")
    else:
        errors.append(f"[g] metrics.json 缺失")

    # [h] selections.parquet 7 行
    sel_p = out_day / "selections.parquet"
    if sel_p.exists():
        sel = pd.read_parquet(sel_p)
        _check(len(sel) == 7, f"[h] selections rows={len(sel)} (期望 7)")
    else:
        errors.append(f"[h] selections.parquet 缺失")

    # [i] webtrader /ml/health (需 token)
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"username": "vnpy", "password": "vnpy"}).encode()
        tok_req = urllib.request.Request("http://127.0.0.1:8001/api/v1/token", data=data)
        with urllib.request.urlopen(tok_req, timeout=5) as r:
            tok = json.loads(r.read())["access_token"]
        req = urllib.request.Request(
            "http://127.0.0.1:8001/api/v1/ml/health",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            health = json.loads(r.read())
        names = [s["name"] for s in health.get("strategies", [])]
        _check(STRATEGY_NAME in names, f"[i,j] webtrader /ml/health 无 {STRATEGY_NAME}, got {names}")
    except Exception as e:
        errors.append(f"[i,j] webtrader REST 访问失败: {e}")

    # [k] SQLite 行数
    if SPAWN_MLEARNWEB and MLEARNWEB_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(MLEARNWEB_DB))
            td_iso_for_sqlite = pd.Timestamp(live_date_str).to_pydatetime()
            n = conn.execute(
                "SELECT COUNT(*) FROM ml_metric_snapshots WHERE strategy_name=? AND trade_date=?",
                (STRATEGY_NAME, td_iso_for_sqlite),
            ).fetchone()[0]
            conn.close()
            _check(n >= 1, f"[k] SQLite ml_metric_snapshots({STRATEGY_NAME}, {live_date_iso}) rows={n}")
        except Exception as e:
            errors.append(f"[k] SQLite 查询失败: {e}")

    # [l] mlearnweb /metrics/rolling
    if SPAWN_MLEARNWEB:
        try:
            import urllib.request
            url = f"http://127.0.0.1:8100/api/live-trading/ml/local/{STRATEGY_NAME}/metrics/rolling?window=30"
            with urllib.request.urlopen(url, timeout=5) as r:
                body = json.loads(r.read())
            hc = (body.get("data") or {}).get("history_count", 0)
            _check(hc >= 1, f"[l] mlearnweb /metrics/rolling history_count={hc}")
        except Exception as e:
            errors.append(f"[l] mlearnweb rolling 访问失败: {e}")

    return errors


def _teardown(main_engine, webtrader_uv, mlearnweb_uv) -> None:
    _log("tearing down...")
    for proc, name in ((webtrader_uv, "webtrader"), (mlearnweb_uv, "mlearnweb")):
        if proc is None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=10)
            _log(f"  {name} uvicorn terminated")
        except subprocess.TimeoutExpired:
            proc.kill()
            _log(f"  {name} uvicorn killed")
        except Exception as e:
            _log(f"  {name} teardown error: {e}")
    try:
        main_engine.close()
    except Exception as e:
        _log(f"  main_engine.close() error: {e}")


if __name__ == "__main__":
    sys.exit(main())
