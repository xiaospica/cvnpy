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
  Phase 3 add_strategy("QlibMLStrategy", ..., trigger_time="21:00") [提前]
  Phase 2+4 每日 ingest+pipeline (单日或 N 日循环, 见 SIMULATE_ROLLING_DAYS)
  Phase 5 等 ml_snapshot_loop tick
  Phase 6 12 条验证断言
  Phase 7 常驻等 Ctrl+C, 清理 2 个 uvicorn 子进程

## 多日模拟 (问题 3)

  SIMULATE_ROLLING_DAYS=N (env) → 从 LIVE_DATE 回溯 N 自然日, 仅保留交易日,
  逐日跑 ingest+pipeline. 每日产出独立的 diagnostics/metrics/selections,
  前端跨天曲线才有数据. N=0 时保持原单日行为.

## 关键修复 (基于 log.log 排查)

  1. Phase 2 ingest 从主线程同步调用改为后台线程+心跳.
     原设计会阻塞 vnpy RPC ~100s+, 触发 webtrader uvicorn 30s timeout → 500.
  2. 各 Stage (fetch/filter/by_stock/dump) 独立记耗时, daily_ingest 返回
     stages_elapsed 便于性能诊断.
  3. Phase 4 subprocess 轮询加心跳 (默认 30s 一次), 长等待期不再静默.

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
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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

# ========== 多日模拟 (问题 3 修复) ==========
# 0 = 只跑 LIVE_DATE 一天 (原行为, 单日冒烟)
# N > 0 = 从 LIVE_DATE 往前回溯 N 个自然日, 只保留交易日, 逐日跑完整 ingest+pipeline.
#         每日结果独立落在 {OUT_ROOT}/{STRATEGY}/{yyyymmdd}/, 前端 rolling 曲线会看到 N+1 个点.
# 也可通过 env SIMULATE_ROLLING_DAYS=N 覆盖.
SIMULATE_ROLLING_DAYS: int = int(os.getenv("SIMULATE_ROLLING_DAYS", "0"))
# 多日模式下, 每跑完一天等 mlearnweb ml_snapshot_loop tick 一次 (60s+余量),
# 让当日 metrics UPSERT 进 SQLite. 0 = 不等 (快但 SQLite 只会留最后一天).
ROLLING_PER_DAY_WAIT_S: int = int(os.getenv("ROLLING_PER_DAY_WAIT_S", "70"))

# ========== Phase 2/4 心跳 (问题 1 修复) ==========
# 后台 ingest 线程每隔 N 秒打心跳, 让用户感知进度
INGEST_HEARTBEAT_S: float = 10.0
# Phase 4 pipeline subprocess 轮询 diagnostics.json 时每隔 N 秒打心跳
PIPELINE_HEARTBEAT_S: float = 30.0
PIPELINE_TIMEOUT_S: int = 300

# 多日模式下 date.today() 的动态 holder (monkey-patch 通过它读)
_SMOKE_DATE_HOLDER: Dict[str, Optional[date]] = {"value": None}

BUNDLE_DIR = r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/f6017411b44c4c7790b63c5766b93964"
OUT_ROOT = r"D:/ml_output/smoke_full_pipeline"
STRATEGY_NAME = "jq41_csi300_2026"

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


TUSHARE_DAILY_READY_HOUR: int = 20


def _resolve_live_date(downloader) -> date:
    """返回最近"已完整收盘"的交易日.

    策略:
      1. env LIVE_DATE 指定 → 尊重
      2. today 是交易日 且 当前时间 >= 20:00 → 用 today
         (tushare 当日 daily bar 通常 20:00 后落盘)
      3. 否则从 today-1 往前找 10 天里最近的交易日
         (白天跑 smoke, tushare 当日数据还没落, 用昨日)
    """
    from datetime import timedelta
    env_val = os.getenv("LIVE_DATE")
    if env_val:
        try:
            return datetime.strptime(env_val, "%Y-%m-%d").date()
        except ValueError:
            _log(f"WARN: env LIVE_DATE={env_val} 格式非法, 忽略")

    def _is_trade_date(d: date) -> bool:
        try:
            return bool(downloader.is_trade_date(d.strftime("%Y%m%d")))
        except Exception:  # noqa: BLE001
            return True  # 查失败保守当交易日

    today = date.today()
    if datetime.now().hour >= TUSHARE_DAILY_READY_HOUR and _is_trade_date(today):
        return today

    # 从 today-1 往前找 10 天里最近的交易日
    candidate = today - timedelta(days=1)
    for _ in range(10):
        if _is_trade_date(candidate):
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


# --- 并发 / 心跳 -------------------------------------------------------

def _run_ingest_with_heartbeat(
    tushare_engine: Any, day_str: str, *, heartbeat_s: float = INGEST_HEARTBEAT_S,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """把 ``run_daily_ingest_now`` 丢到后台线程跑, 主线程周期性打心跳.

    原本 ingest 在主线程同步执行, qlib DumpDataAll 的 I/O 会卡住主线程 ~100s+,
    期间 webtrader RPC server 无法及时响应 → uvicorn 30s 超时抛 500
    (见 F:/log.log 行 142 `RpcServer has no response over 30 seconds`).
    放后台线程后主线程保持空转, RPC 事件循环不被阻塞.

    Parameters
    ----------
    tushare_engine : TushareProEngine
    day_str : str
        YYYYMMDD
    heartbeat_s : float
        心跳间隔秒

    Returns
    -------
    (result_dict, elapsed_s): ingest 返回的 dict (可能为 None/skipped), 总耗时
    """
    state: Dict[str, Any] = {"result": None, "error": None, "done": False}

    def _worker() -> None:
        try:
            state["result"] = tushare_engine.run_daily_ingest_now(day_str)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            state["done"] = True

    th = threading.Thread(target=_worker, daemon=False, name=f"ingest-{day_str}")
    t0 = time.time()
    th.start()
    last_hb = t0
    while not state["done"]:
        time.sleep(0.5)
        now = time.time()
        if now - last_hb >= heartbeat_s:
            _log(f"  ... ingest[{day_str}] running ({now - t0:.0f}s elapsed)")
            last_hb = now
    th.join(timeout=5)
    if state["error"] is not None:
        raise state["error"]
    return state["result"], time.time() - t0


def _wait_pipeline_with_heartbeat(
    ml_engine: Any,
    strategy_name: str,
    day_str: str,
    out_root: str,
    *,
    timeout_s: int = PIPELINE_TIMEOUT_S,
    heartbeat_s: float = PIPELINE_HEARTBEAT_S,
) -> Tuple[bool, float]:
    """触发 ``run_pipeline_now`` 后轮询 ``diagnostics.json``, 每隔 heartbeat_s 打进度.

    注意: 用 ``mtime > initial_mtime`` 判断"新产出", 否则如果该日之前跑过,
    旧 diagnostics.json 会立刻命中, 无法正确等待本次运行.

    Returns
    -------
    (ok, elapsed_s): ok=True 表示 diagnostics 在 timeout 内被本次运行重写
    """
    out_day_dir = Path(out_root) / strategy_name / day_str
    diag_path = out_day_dir / "diagnostics.json"
    initial_mtime = diag_path.stat().st_mtime if diag_path.exists() else 0.0

    # t0 必须在 run_pipeline_now 之前: 该函数实测为"同步阻塞直到 subprocess
    # 完成"(见 smoke 日志 scheduler start→job done ~74s), 若 t0 放后面,
    # elapsed 只计了 subprocess 完成后的 mtime 轮询时间(~2s), 严重误导.
    t0 = time.time()
    if not ml_engine.run_pipeline_now(strategy_name):
        raise RuntimeError(f"run_pipeline_now failed for {day_str}")

    last_hb = t0
    while time.time() - t0 < timeout_s:
        time.sleep(2)
        if diag_path.exists() and diag_path.stat().st_mtime > initial_mtime:
            return True, time.time() - t0
        now = time.time()
        if now - last_hb >= heartbeat_s:
            _log(
                f"  ... pipeline[{day_str}] running ({now - t0:.0f}s elapsed, "
                f"waiting for {diag_path.name})"
            )
            last_hb = now
    return False, time.time() - t0


def _run_day(
    tushare_engine: Any,
    ml_engine: Any,
    day: date,
) -> bool:
    """跑一天的 ingest + pipeline (把 Phase 2 + Phase 4 合到一起).

    Returns
    -------
    bool: True = 本日成功完成 (或非交易日 skipped, 不算失败);
          False = 本日出现 fatal 错误, 调用方可决定是否中断多日循环
    """
    day_str = day.strftime("%Y%m%d")
    day_iso = day.strftime("%Y-%m-%d")
    _SMOKE_DATE_HOLDER["value"] = day
    _log(f"--- Day {day_iso} ({day_str}) ---")

    # Phase 2: ingest (后台线程, 不阻塞 RPC)
    if TRIGGER_INGEST_ON_STARTUP:
        _log(f"[{day_str}] Phase 2 — DailyIngestPipeline.ingest_today (bg thread)")
        try:
            result, elapsed = _run_ingest_with_heartbeat(tushare_engine, day_str)
        except Exception as exc:  # noqa: BLE001
            _log(f"  FAIL: ingest 抛异常: {type(exc).__name__}: {exc}")
            return False
        if result is None:
            _log("  FAIL: run_daily_ingest_now 返回 None (pipeline 未配置)")
            return False
        if result.get("skipped"):
            _log(f"  SKIPPED: {day_str} 非交易日, 跳过本日流程")
            return True  # 非交易日不算 fatal
        stages = result.get("stages_elapsed", {})
        stage_parts = [
            f"{s}={stages.get(s, 0):.1f}s"
            for s in ("fetch", "filter", "by_stock", "dump")
            if s in stages
        ]
        stages_str = " | ".join(stage_parts) if stage_parts else "no-stage-data"
        _log(
            f"  ingest OK merged_rows={result['merged_rows']} "
            f"filtered_today_rows={result.get('filtered_today_rows')} "
            f"stages=[{stages_str}] total={elapsed:.1f}s"
        )
    else:
        _log(f"[{day_str}] Phase 2 skipped (TRIGGER_INGEST_ON_STARTUP=False)")

    # Phase 4: pipeline (subprocess, 主线程轮询 diagnostics)
    if TRIGGER_PIPELINE_ON_STARTUP:
        _log(f"[{day_str}] Phase 4 — run_pipeline_now")
        try:
            ok, elapsed = _wait_pipeline_with_heartbeat(
                ml_engine, STRATEGY_NAME, day_str, OUT_ROOT,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"  FAIL: pipeline 抛异常: {type(exc).__name__}: {exc}")
            return False
        if ok:
            _log(f"  pipeline done elapsed={elapsed:.1f}s")
        else:
            _log(
                f"  TIMEOUT: pipeline 未在 {PIPELINE_TIMEOUT_S}s 内产出新 diagnostics "
                f"(elapsed={elapsed:.1f}s)"
            )
            return False
    else:
        _log(f"[{day_str}] Phase 4 skipped (TRIGGER_PIPELINE_ON_STARTUP=False)")

    return True


def _build_rolling_days(live_date: date, n_days: int, downloader: Any) -> list[date]:
    """从 live_date 往前 n_days 个自然日, 只保留交易日 (含 live_date 本身)."""
    candidates = [live_date - timedelta(days=i) for i in range(n_days, -1, -1)]
    days: list[date] = []
    for d in candidates:
        try:
            if bool(downloader.is_trade_date(d.strftime("%Y%m%d"))):
                days.append(d)
        except Exception:  # noqa: BLE001
            days.append(d)  # 查失败保守保留
    return days


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
    today = date.today()
    if live_date == today:
        _log(f"LIVE_DATE={live_date} (today, tushare 当日 bar 已落)")
    elif os.getenv("LIVE_DATE"):
        _log(f"LIVE_DATE={live_date} (来自 env)")
    elif datetime.now().hour < TUSHARE_DAILY_READY_HOUR:
        _log(
            f"LIVE_DATE={live_date}: 当前 {datetime.now():%H:%M} < "
            f"{TUSHARE_DAILY_READY_HOUR}:00, tushare 当日 bar 未落, 回退 today-1"
        )
    else:
        _log(f"LIVE_DATE={live_date}: today={today} 非交易日, 回退到最近交易日")

    # 无条件 monkey-patch: 单日模式 holder=live_date 行为不变; 多日循环模式
    # 每次 _run_day 会更新 holder.  template.run_daily_pipeline 用
    # date.today() 决定 live_end + trade_day 短路, 必须动态.
    _SMOKE_DATE_HOLDER["value"] = live_date
    import vnpy_ml_strategy.template as _tpl_mod
    _orig_date = _tpl_mod.date

    class _SmokeDate(_orig_date):
        @classmethod
        def today(cls):
            return _SMOKE_DATE_HOLDER["value"] or _orig_date.today()
    _tpl_mod.date = _SmokeDate
    _log(
        "monkey-patched vnpy_ml_strategy.template.date.today() -> dynamic "
        f"(initial={live_date})"
    )

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

    # --- Phase 3: add + init + start 策略 (提前到 Phase 2 之前, 以便多日
    # 循环模式下策略已就绪, 每日 run_pipeline_now 直接复用同一策略实例) ---
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

    # --- Phase 2 + Phase 4: 每日 ingest + pipeline ---
    # SIMULATE_ROLLING_DAYS=0 → 仅 LIVE_DATE (原行为)
    # SIMULATE_ROLLING_DAYS>0 → 回溯 N 天逐日跑, 每日产出独立 diagnostics/metrics/selections
    #                            让前端跨天曲线有数据.
    if SIMULATE_ROLLING_DAYS > 0:
        days = _build_rolling_days(live_date, SIMULATE_ROLLING_DAYS, downloader)
        if not days:
            _log(f"FAIL: 回溯 {SIMULATE_ROLLING_DAYS} 天内没有交易日")
            _teardown(main_engine, webtrader_uv, mlearnweb_uv)
            return 3
        _log(
            f"=== Phase 2/4 — Rolling simulation: {len(days)} 交易日 "
            f"[{days[0]} ~ {days[-1]}] (自然日回溯 {SIMULATE_ROLLING_DAYS}) ==="
        )
    else:
        days = [live_date]
        _log("=== Phase 2/4 — 单日模式 ===")

    for idx, day in enumerate(days):
        is_last = idx == len(days) - 1
        _log(f"=== Day {idx + 1}/{len(days)} ===")
        ok = _run_day(tushare_engine, ml_engine, day)
        if not ok:
            _log(f"FAIL: Day {day} 失败, 中断多日循环")
            _teardown(main_engine, webtrader_uv, mlearnweb_uv)
            return 3
        # 多日模式下, 每日跑完等 ml_snapshot_loop tick 一次, 让当日 metrics
        # UPSERT 进 SQLite (否则 mlearnweb 只会采到最后一天 latest)
        if (
            not is_last
            and SIMULATE_ROLLING_DAYS > 0
            and SPAWN_MLEARNWEB
            and TRIGGER_PIPELINE_ON_STARTUP
            and ROLLING_PER_DAY_WAIT_S > 0
        ):
            _log(f"  等 mlearnweb ml_snapshot_loop tick ({ROLLING_PER_DAY_WAIT_S}s)...")
            time.sleep(ROLLING_PER_DAY_WAIT_S)

    # --- Phase 5: 等 ml_snapshot_loop tick (最后一天) ---
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

    # [e] calendars/day.txt — 严格校验末尾 == live_date_iso. by_stock 来自
    # T 冻结的 merged snapshot (Stage 1 严格过滤 trade_date<=T),
    # DumpDataAll 出的 calendar 末尾必定 == T. 不等于说明快照被污染.
    cal_path = Path(PROVIDER_URI) / "calendars" / "day.txt"
    if cal_path.exists():
        lines = [l for l in cal_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        _check(
            lines and lines[-1] == live_date_iso,
            f"[e] calendars last={lines[-1] if lines else '?'} != {live_date_iso}",
        )
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
    # SQLAlchemy 在 Python 3.12 以后 datetime TEXT 存储多了微秒位
    # ("2026-04-17 00:00:00.000000"), Python sqlite3 默认 datetime adapter
    # 写出来是 "2026-04-17 00:00:00" — 直接 =? 比较会漏. 用 LIKE 前缀匹配
    # 规避日期 TEXT 存储精度问题.
    if SPAWN_MLEARNWEB and MLEARNWEB_DB.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(MLEARNWEB_DB))
            n = conn.execute(
                "SELECT COUNT(*) FROM ml_metric_snapshots "
                "WHERE strategy_name=? AND trade_date LIKE ?",
                (STRATEGY_NAME, f"{live_date_iso}%"),
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
