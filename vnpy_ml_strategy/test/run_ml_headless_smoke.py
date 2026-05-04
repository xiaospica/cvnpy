# -*- coding: utf-8 -*-
"""ML 策略多日加速测试脚本 — run_ml_headless 的"测试增强孪生".

## 测什么

run_ml_headless.py 启动后内部已有 batch replay 机制 (策略 on_start 后台触发
``run_inference_range`` 一次性跑完 [replay_start_date, today-1]), 但实时
模拟阶段的"定时拉数 → 定时推理 → 定时交易"循环只能等真实 21:00 cron 才能验证.
本脚本: 复用策略原生 batch replay + smoke 主线程接管最近 N 天的 single-day
循环 (每日 ingest + run_pipeline_now), 验证两段端到端流程.

## 阶段切分 (核心契约)

参数 ``SMOKE_LIVE_DAYS = N`` (默认 3):

  - 回放段 (策略 on_start 自动 batch replay):
        范围 = [replay_start_date, live_days[0] - 1]
        实现 = 策略 _start_replay_if_needed → 后台线程 _run_replay_loop →
               signal_engine.run_inference_range(start, end) **一次** spawn subprocess
        产物 = 每日 batch_mode=true 的 diagnostics + metrics + selections + predictions
        smoke 通过 setting override 注入 ``replay_end_date = live_days[0] - 1``
        让策略 batch 提前停在实时段前一天.
        与 run_ml_headless 启动时行为完全相同代码路径.

  - 实时段 (smoke 主线程 single-day 循环):
        范围 = 最近 N 个交易日 (从 live_max = resolve_live_date() 倒推)
        实现 = for day in live_days:
                   run_daily_ingest_now(day)                # 每日真触发 ingest
                   run_pipeline_now(name, as_of_date=day)   # single-day pipeline
                   sleep(SMOKE_LIVE_SECONDS_PER_DAY)        # 让 mlearnweb 采样
        产物 = 每日 batch_mode=false 的完整四件套
        与生产 21:00 cron 触发完全相同代码路径.

边界: SMOKE_LIVE_DAYS=0 → 全部归 batch replay (无 single-day 段),
等价于 run_ml_headless 默认启动行为, 用作等价性测试 baseline.

## 不 monkey-patch date.today()

run_pipeline_now(as_of_date=) 已支持显式日期注入 (engine.py:239), 所有下游
从 today=as_of_date 派生 (template.py:345), 无需 patch.

## 与 run_ml_headless 等价性

smoke (SMOKE_LIVE_DAYS=3) vs run_ml_headless (SMOKE_LIVE_DAYS=0 等价启动)
跑出的产物应严格一致, 验证工具:
    F:/Program_Home/vnpy/python.exe \\
        vnpy_ml_strategy/test/diff_smoke_vs_headless.py OUT_ROOT_A OUT_ROOT_B

## 运行

```
# 默认: batch replay 全段 + single-day 最近 3 天
F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# baseline B: 全 batch replay (无 single-day, 等价 run_ml_headless 启动)
SMOKE_LIVE_DAYS=0 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 调实时段长度
SMOKE_LIVE_DAYS=5 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 关下单
SMOKE_ENABLE_TRADING=0 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 关 mlearnweb 子进程
SMOKE_SPAWN_MLEARNWEB=0 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py
```

## 与 smoke_full_pipeline.py 区别

  smoke_full_pipeline:  单策略硬编码 jq41_csi300_2026, 默认 1 天 ingest+推理
  run_ml_headless_smoke: 复用 run_ml_headless 的多策略多 gateway 配置,
                         先 batch replay 再 single-day, 多策略串行驱动

两者都用同一份 _pipeline_drivers helper (实时段 single-day 部分).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple


# =====================================================================
# stdout 强制 UTF-8 (Windows 默认 GBK 不能编码 ✓ 等 unicode 符号)
# 必须在第一次 print 之前执行
# =====================================================================
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — 旧 Python / non-tty 环境兜底
    pass


# =====================================================================
# sys.path 注入 (与 run_ml_headless 一致)
# =====================================================================

os.environ["VNPY_DOCK_BACKEND"] = "ads"
_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
_QLIB_SOURCE = Path(os.getenv("QLIB_SOURCE_ROOT", r"F:\Quant\code\qlib_strategy_dev"))
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists() and str(_QLIB_SOURCE) not in sys.path:
    sys.path.insert(0, str(_QLIB_SOURCE))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# =====================================================================
# 复用 run_ml_headless 的配置 (避免双份漂移)
# =====================================================================

from run_ml_headless import (  # noqa: E402  (sys.path 必须先注入)
    GATEWAYS,
    STRATEGIES,
    STRATEGY_BASE_SETTING,
    USE_GATEWAY_KIND,
    _validate_startup_config,
)
from vnpy_ml_strategy.test._pipeline_drivers import (  # noqa: E402
    assert_orders_for_gateway,
    assert_strategy_day_outputs,
    resolve_live_date,
    run_ingest_with_heartbeat,
    wait_pipeline_with_heartbeat,
)


# =====================================================================
# 配置区
# =====================================================================

# --- 1) 阶段切分 ---
# SMOKE_LIVE_DAYS = N (默认 3):
#   - 回放段 = 策略 on_start 自动 batch replay [replay_start, live_days[0]-1]
#             (smoke 通过 setting 注入 replay_end_date 让 batch 提前停)
#   - 实时段 = smoke 主线程 single-day 循环最近 N 个交易日
#             (从 live_max = resolve_live_date() 倒推 N 个)
# SMOKE_LIVE_DAYS=0 → 全 batch replay 跑到 today-1, 无 single-day 段
#                    (等价 run_ml_headless 默认启动行为, 用作等价性 baseline)
SMOKE_LIVE_DAYS: int = int(os.getenv("SMOKE_LIVE_DAYS", "3"))

# 实时段每日尾 sleep (秒) — 让 mlearnweb ml_snapshot_loop 60s tick 至少触发一次
SMOKE_LIVE_SECONDS_PER_DAY: int = int(os.getenv("SMOKE_LIVE_SECONDS_PER_DAY", "30"))

# --- 2) 行为开关 ---
# 是否每日真触发 DailyIngestPipeline (已有当日 ingest 时可关)
SMOKE_DO_INGEST: bool = os.getenv("SMOKE_DO_INGEST", "1") != "0"

# 是否派生 mlearnweb live_main uvicorn 子进程
SMOKE_SPAWN_MLEARNWEB: bool = os.getenv("SMOKE_SPAWN_MLEARNWEB", "1") != "0"

# 是否真实下单到 sim gateway (覆盖 STRATEGY_BASE_SETTING 默认值)
SMOKE_ENABLE_TRADING: bool = os.getenv("SMOKE_ENABLE_TRADING", "1") != "0"

# 是否在常驻段等 Ctrl+C (False 时跑完断言直接退出, 适合 CI)
SMOKE_KEEP_ALIVE: bool = os.getenv("SMOKE_KEEP_ALIVE", "1") != "0"

# --- 3) 心跳 / 超时 ---
INGEST_HEARTBEAT_S: float = float(os.getenv("SMOKE_INGEST_HEARTBEAT_S", "10"))
PIPELINE_HEARTBEAT_S: float = float(os.getenv("SMOKE_PIPELINE_HEARTBEAT_S", "30"))
PIPELINE_TIMEOUT_S: int = int(os.getenv("SMOKE_PIPELINE_TIMEOUT_S", "600"))

# --- 4) 路径 / 解释器 ---
QS_DATA_ROOT: str = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
PY311: str = os.getenv(
    "INFERENCE_PYTHON",
    r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe",
)
MLEARNWEB_BACKEND: str = r"F:/Quant/code/qlib_strategy_dev/mlearnweb/backend"
SIM_DB_DIR: str = r"F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state"

# --- 5) ingest 环境变量 (复用 smoke_full_pipeline 同一套) ---
if "ML_INGEST_LOOKBACK_DAYS" not in os.environ:
    os.environ["ML_INGEST_LOOKBACK_DAYS"] = "250"

# DumpDataAll ProcessPoolExecutor max_workers 默认 4 (ml_data_build 默认 8).
# Windows spawn 模式每 worker 占 500MB-1GB commit memory, 4 worker 内存峰值
# 比 8 worker 减半. CSV→bin 是 IO bound, 4 worker 已能让多核充分利用.
os.environ.setdefault("ML_INGEST_DUMP_WORKERS", "4")

WEBTRADER_HTTP_PORT: int = 8001
MLEARNWEB_PORT: int = 8100


# =====================================================================
# 工具
# =====================================================================


def _log(msg: str) -> None:
    print(f"[smoke_headless] {msg}", flush=True)


def _load_tushare_token() -> str:
    """从 ``api.json`` 读 tushare token."""
    import json as _json

    api_json = _HERE / "api.json"
    if not api_json.exists():
        raise FileNotFoundError(f"api.json 不存在: {api_json}")
    data = _json.loads(api_json.read_text(encoding="utf-8"))
    token = data.get("token") or data.get("password") or data.get("tushare_token")
    if not token:
        raise RuntimeError(f"api.json 里找不到 tushare token: {list(data.keys())}")
    return token


def _setup_ingest_env(token: str) -> None:
    """把 DailyIngestPipeline 需要的 env 变量塞进 os.environ."""
    os.environ["TUSHARE_TOKEN"] = token
    os.environ["ML_DAILY_INGEST_ENABLED"] = "1"
    os.environ["QS_DATA_ROOT"] = QS_DATA_ROOT
    # 兜底显式设 ML_* 路径 (与 smoke_full_pipeline 一致)
    os.environ.setdefault("ML_MERGED_PARQUET_PATH", f"{QS_DATA_ROOT}/stock_data/daily_merged_all_new.parquet")
    os.environ.setdefault("ML_FILTERED_PARQUET_PATH", f"{QS_DATA_ROOT}/csi300_custom_filtered.parquet")
    os.environ.setdefault("ML_BY_STOCK_CSV_DIR", f"{QS_DATA_ROOT}/stock_data/by_stock")
    os.environ.setdefault("ML_QLIB_DIR", f"{QS_DATA_ROOT}/qlib_data_bin")
    os.environ.setdefault("ML_SNAPSHOT_DIR", f"{QS_DATA_ROOT}/snapshots")
    if "ML_JQ_INDEX_CSV_PATHS" not in os.environ:
        import json as _json
        os.environ["ML_JQ_INDEX_CSV_PATHS"] = _json.dumps(
            {"csi300": f"{QS_DATA_ROOT}/jq_index/hs300_*.csv"}
        )
    os.environ.setdefault("VNPY_DATAFEED_USERNAME", "tushare")
    os.environ.setdefault("VNPY_DATAFEED_PASSWORD", token)


def _spawn_uvicorn(
    label: str,
    python_exe: str,
    asgi_target: str,
    port: int,
    cwd: str,
    extra_env: Optional[dict] = None,
) -> subprocess.Popen:
    """通用派生 uvicorn 子进程."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [python_exe, "-u", "-m", "uvicorn", asgi_target,
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=cwd,
        env=env,
    )
    _log(f"spawned {label} uvicorn pid={proc.pid} on :{port}")
    return proc


def _teardown_uvicorn(proc: Optional[subprocess.Popen], label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
        _log(f"  {label} uvicorn terminated")
    except subprocess.TimeoutExpired:
        proc.kill()
        _log(f"  {label} uvicorn killed (force)")
    except Exception as exc:  # noqa: BLE001
        _log(f"  {label} teardown error: {exc}")


# =====================================================================
# 主流程
# =====================================================================


def main() -> int:
    _log("=== Phase 0 — 前置 ===")
    _validate_startup_config()
    token = _load_tushare_token()
    _setup_ingest_env(token)

    _log("=== Phase 1 — vnpy 全栈 ===")
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.setting import SETTINGS

    SETTINGS["datafeed.username"] = "tushare"
    SETTINGS["datafeed.password"] = token

    from vnpy_ml_strategy import MLStrategyApp, APP_NAME as ML_APP
    from vnpy_tushare_pro import TushareProApp
    from vnpy_tushare_pro.engine import APP_NAME as TUSHARE_APP
    from vnpy_webtrader import WebTraderApp
    from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # Gateway
    if USE_GATEWAY_KIND == "QMT_SIM":
        from vnpy_qmt_sim import QmtSimGateway as _GatewayClass
    else:
        from vnpy_qmt import QmtGateway as _GatewayClass
    for gw in GATEWAYS:
        main_engine.add_gateway(_GatewayClass, gateway_name=gw["name"])

    # Apps
    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)
    main_engine.add_app(WebTraderApp)

    # 连接所有 gateway
    for gw in GATEWAYS:
        _log(f"connecting gateway {gw['name']}...")
        main_engine.connect(gw["setting"], gw["name"])
    time.sleep(2)

    # Engines
    tushare_engine = main_engine.get_engine(TUSHARE_APP)
    tushare_engine.init_engine()
    pipeline_ready = tushare_engine._get_tushare_datafeed().daily_ingest_pipeline is not None
    _log(f"TushareProEngine inited, daily_ingest_pipeline_ready={pipeline_ready}")
    if SMOKE_DO_INGEST and not pipeline_ready:
        _log("FAIL: SMOKE_DO_INGEST=1 但 daily_ingest_pipeline 未配置 (检查 ML_* env)")
        main_engine.close()
        return 2

    ml_engine = main_engine.get_engine(ML_APP)
    ml_engine.init_engine()
    _log(f"MLEngine inited, classes={ml_engine.get_all_strategy_class_names()}")

    web_engine = main_engine.get_engine(WEB_APP_NAME)
    web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
    _log("webtrader RPC :2014 / :4102")

    # 派生 webtrader uvicorn (mlearnweb 通过它拉数据)
    webtrader_uv = _spawn_uvicorn(
        "webtrader", sys.executable, "vnpy_webtrader.web:app",
        WEBTRADER_HTTP_PORT, str(_HERE),
    )

    # 派生 mlearnweb live_main uvicorn (用 PY311, 与 vnpy 主进程解释器解耦)
    mlearnweb_uv: Optional[subprocess.Popen] = None
    if SMOKE_SPAWN_MLEARNWEB:
        mlearnweb_uv = _spawn_uvicorn(
            "mlearnweb", PY311, "app.live_main:app",
            MLEARNWEB_PORT, MLEARNWEB_BACKEND,
            extra_env={
                "ML_LIVE_OUTPUT_ROOT": STRATEGY_BASE_SETTING.get("output_root", r"D:/ml_output"),
                "VNPY_SNAPSHOT_RETENTION_DAYS": "365",
            },
        )

    time.sleep(4)
    if webtrader_uv.poll() is not None:
        _log(f"FAIL: webtrader uvicorn died early (rc={webtrader_uv.returncode})")
        return 2

    # --- Phase 2: 计算 live_days + replay_end (注入 add_strategy 之前) ---
    _log("=== Phase 2 — 计算阶段切分 ===")
    downloader = tushare_engine._get_tushare_datafeed().downloader

    def _is_trade_date_fn(d: date) -> bool:
        return bool(downloader.is_trade_date(d.strftime("%Y%m%d")))

    today = date.today()
    live_max = resolve_live_date(_is_trade_date_fn)
    _log(f"  today={today}, live_max={live_max} (最近已完整收盘交易日)")

    # live_days = 从 live_max 倒推 SMOKE_LIVE_DAYS 个交易日 (含 live_max)
    if SMOKE_LIVE_DAYS > 0:
        live_days: List[date] = []
        cursor = live_max
        scanned = 0
        while len(live_days) < SMOKE_LIVE_DAYS and scanned < 60:
            try:
                if _is_trade_date_fn(cursor):
                    live_days.insert(0, cursor)
            except Exception:  # noqa: BLE001 — 查失败保守保留
                live_days.insert(0, cursor)
            cursor -= timedelta(days=1)
            scanned += 1
        if not live_days:
            _log(f"FAIL: live_max={live_max} 倒推 {SMOKE_LIVE_DAYS} 天没找到交易日")
            _teardown_uvicorn(webtrader_uv, "webtrader")
            _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
            main_engine.close()
            return 2
        replay_end = live_days[0] - timedelta(days=1)  # 回放段终点 = 实时段第一天前一天
        _log(
            f"  实时段 {len(live_days)} 日 [{live_days[0]} ~ {live_days[-1]}] "
            f"(smoke single-day per day)"
        )
        _log(f"  回放段终点 replay_end_date={replay_end} (策略 batch replay 跑到这天)")
    else:
        live_days = []
        replay_end = live_max
        _log(
            f"  SMOKE_LIVE_DAYS=0, 全 batch replay 到 {replay_end}, 无 single-day 段 "
            f"(等价 run_ml_headless 默认启动)"
        )

    # --- Phase 3: add + init + start 所有策略 (注入 replay_end_date) ---
    # 策略 on_start 会自动后台触发 batch replay [replay_start_date, replay_end_date]
    _log("=== Phase 3 — add_strategy + 触发 batch replay ===")
    valid_gw = {gw["name"] for gw in GATEWAYS}
    started: List[Tuple[str, str]] = []

    for strat_def in STRATEGIES:
        name = strat_def["strategy_name"]
        cls = strat_def["strategy_class"]
        gw_name = strat_def["gateway_name"]
        if gw_name not in valid_gw:
            _log(f"  策略 {name} gateway={gw_name} 未注册, 跳过")
            continue

        setting = {
            **STRATEGY_BASE_SETTING,
            **strat_def["setting_override"],
            "gateway": gw_name,
            # smoke 强制覆盖
            "enable_trading": SMOKE_ENABLE_TRADING,
            "subprocess_timeout_s": PIPELINE_TIMEOUT_S,
            # 关键: 注入 replay_end_date 让策略 batch replay 自动停在该日,
            # smoke Phase 5 接管 single-day 实时段
            "replay_end_date": replay_end.strftime("%Y-%m-%d"),
        }
        _log(f"  adding {name} → gateway={gw_name} replay_end={replay_end} trading={SMOKE_ENABLE_TRADING}")
        try:
            ml_engine.add_strategy(cls, name, setting)
        except Exception as exc:  # noqa: BLE001
            _log(f"  add_strategy({name}) failed: {exc}")
            continue
        if not ml_engine.init_strategy(name):
            _log(f"  init_strategy({name}) failed")
            continue
        if not ml_engine.start_strategy(name):
            _log(f"  start_strategy({name}) failed")
            continue
        started.append((name, gw_name))

    if not started:
        _log("FAIL: no strategy started")
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 2

    _log(f"  {len(started)} 策略已启动: {[s[0] for s in started]}")

    # --- Phase 4: 等所有策略 batch replay 完成 ---
    # 策略 on_start 启动后台线程跑 _run_replay_loop, 主线程立即返回.
    # smoke 主线程 poll strategy.replay_status 直到全部进入终态.
    # 终态: completed / skipped_live / error
    # (template.py:1065/977/989/1068; "running" 是中间态)
    _log("=== Phase 4 — 等 batch replay 完成 ===")
    poll_t0 = time.time()
    poll_interval = 5.0
    last_heartbeat = poll_t0
    BATCH_TIMEOUT_S = int(os.getenv("SMOKE_BATCH_REPLAY_TIMEOUT_S", "1800"))  # 默认 30 分钟
    terminal_states = {"completed", "skipped_live", "error"}

    while True:
        elapsed = time.time() - poll_t0
        statuses = []
        all_terminal = True
        for name, _gw in started:
            strat = ml_engine.strategies.get(name)
            status = getattr(strat, "replay_status", "unknown") if strat else "missing"
            statuses.append(f"{name}={status}")
            if status not in terminal_states:
                all_terminal = False

        if all_terminal:
            _log(f"  batch replay 全部终态 (elapsed={elapsed:.0f}s): {' | '.join(statuses)}")
            break

        if elapsed > BATCH_TIMEOUT_S:
            _log(f"FAIL: batch replay 超时 {BATCH_TIMEOUT_S}s, 状态: {' | '.join(statuses)}")
            _teardown_uvicorn(webtrader_uv, "webtrader")
            _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
            main_engine.close()
            return 3

        now = time.time()
        if now - last_heartbeat >= 30.0:
            _log(f"  ... batch replay running (elapsed={elapsed:.0f}s) {' | '.join(statuses)}")
            last_heartbeat = now
        time.sleep(poll_interval)

    # 检查是否有策略 replay error
    for name, _gw in started:
        strat = ml_engine.strategies.get(name)
        status = getattr(strat, "replay_status", "unknown") if strat else "missing"
        if status == "error":
            err = getattr(strat, "last_error", "?")
            _log(f"FAIL: 策略 {name} batch replay error: {err}")
            _teardown_uvicorn(webtrader_uv, "webtrader")
            _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
            main_engine.close()
            return 3

    # --- Phase 5: 实时段 single-day 循环 (仅 SMOKE_LIVE_DAYS > 0 时执行) ---
    if live_days:
        _log(f"=== Phase 5 — 实时段 single-day 循环 ({len(live_days)} 天) ===")
        for idx, day in enumerate(live_days):
            is_last = idx == len(live_days) - 1
            day_str = day.strftime("%Y%m%d")
            _log(f"--- Live Day {idx + 1}/{len(live_days)} {day} ({day_str}) ---")

            # Phase 5.1 — Ingest (实时段每日真触发)
            if SMOKE_DO_INGEST:
                try:
                    result, elapsed = run_ingest_with_heartbeat(
                        tushare_engine, day_str,
                        heartbeat_s=INGEST_HEARTBEAT_S, log_fn=_log,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log(f"  FAIL: ingest 抛异常: {type(exc).__name__}: {exc}")
                    _teardown_uvicorn(webtrader_uv, "webtrader")
                    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                    main_engine.close()
                    return 3
                if result is None:
                    _log("  FAIL: run_daily_ingest_now 返回 None")
                    _teardown_uvicorn(webtrader_uv, "webtrader")
                    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                    main_engine.close()
                    return 3
                if result.get("skipped"):
                    _log(f"  SKIPPED: {day} 非交易日, 跳过本日 single-day")
                    continue
                stages = result.get("stages_elapsed", {})
                stage_parts = [f"{s}={stages.get(s, 0):.1f}s" for s in
                               ("fetch", "filter", "by_stock", "dump") if s in stages]
                _log(
                    f"  ingest OK merged_rows={result.get('merged_rows')} "
                    f"filtered_today_rows={result.get('filtered_today_rows')} "
                    f"stages=[{' | '.join(stage_parts)}] total={elapsed:.1f}s"
                )

                # Phase 5.1.b — Ingest 后强制释放内存 (防 OOM)
                # 根因: DumpDataAll ProcessPoolExecutor 每 worker 占 500MB-1GB
                # commit memory, Windows 子进程退出后 OS 异步回收 page file 不立即归还.
                # 多次 ingest 累积 → MemoryError.
                # 缓解: gc.collect() 主动释放 Python heap + sleep 让 OS 自然回收.
                # 配套: ML_INGEST_DUMP_WORKERS 默认 4 (smoke 顶层 setdefault)
                # 把内存峰值减半. cooldown 默认 10s — 假设 page file 已配 16GB+,
                # 25GB+ page file 下 cooldown=10s 已充足让 OS 回收.
                import gc
                gc.collect()
                sleep_s = int(os.getenv("SMOKE_INGEST_COOLDOWN_S", "10"))
                if sleep_s > 0:
                    _log(f"  ingest cooldown {sleep_s}s (gc.collect + 等 OS 回收 page file)")
                    time.sleep(sleep_s)

            # Phase 5.2 — Ingest 后刷新 ml_engine 的 trade_calendar 缓存
            # 必要性: ingest 内 DumpDataAll 重写 D:/vnpy_data/qlib_data_bin/calendars/day.txt,
            # 但 QlibCalendar 实例在首次 _load() 时缓存了 trade_days set, 后续不重读.
            # 不刷新会导致策略 _is_trade_day(day) 用陈旧 cache, 走 non-trading day 路径
            # 直接 skip 不写 diagnostics, smoke 会被错误地认为有 bug.
            try:
                cal = getattr(ml_engine, "_trade_calendar", None)
                if cal is not None and hasattr(cal, "refresh"):
                    cal.refresh()
                    _log(f"  trade_calendar refreshed (after ingest {day_str})")
            except Exception as exc:  # noqa: BLE001
                _log(f"  WARN: trade_calendar.refresh() failed: {exc}")

            # Phase 5.3 — 模拟 09:26 cron (buy_sell_time): 多策略串行 rebalance
            # 双 cron 架构:
            #   - 09:26 cron: run_open_rebalance(strat) → 读昨日 pred + 刷今日 tick + rebalance + send_order
            #   - 09:30 撮合 (vnpy_qmt_sim 同步)
            #   - settle EOD (smoke 显式调, 生产由 gateway timer 跨自然日触发)
            #   - 21:00 cron: run_pipeline_now(strat, as_of_date=day) → 推理 + persist
            # 这样与 batch replay [Day=T] iter 语义等价 (用 prev_day_pred 撮合).
            _log(f"  [09:26 cron] 多策略 rebalance (用上日 21:00 persist 的 pred)")
            for strat_name, _gw in started:
                try:
                    ok = ml_engine.run_open_rebalance_now(strat_name, as_of_date=day)
                    if not ok:
                        _log(f"  WARN: run_open_rebalance_now({strat_name}) 返 False")
                except Exception as exc:  # noqa: BLE001
                    _log(f"  FAIL: 09:26 rebalance[{strat_name}] 异常: {type(exc).__name__}: {exc}")
                    _teardown_uvicorn(webtrader_uv, "webtrader")
                    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                    main_engine.close()
                    return 3

            # Phase 5.4 — 09:30 撮合后 EOD settle (T+1 持仓结转 + mark-to-market)
            # 生产环境 gateway timer 按自然日 wall-clock 跨切自动 settle, smoke
            # fast-forward wall-clock 不跨日, auto-settle 不触发, 显式调.
            for strat_name, gw_name in started:
                gw = main_engine.get_gateway(gw_name)
                counter = getattr(getattr(gw, "td", None), "counter", None)
                if counter is None or not hasattr(counter, "settle_end_of_day"):
                    continue
                try:
                    counter.settle_end_of_day(day)
                    _log(f"  [{gw_name}] settle_end_of_day({day}) ok")
                except Exception as exc:  # noqa: BLE001
                    _log(f"  WARN: {gw_name} settle({day}) 异常: {type(exc).__name__}: {exc}")

            # Phase 5.5 — 模拟 21:00 cron (trigger_time): 多策略串行 推理 + persist
            _log(f"  [21:00 cron] 多策略 推理 + persist selections.parquet (无下单)")
            for strat_name, _gw in started:
                try:
                    status, elapsed = wait_pipeline_with_heartbeat(
                        ml_engine, strat_name, day,
                        output_root=STRATEGY_BASE_SETTING["output_root"],
                        timeout_s=PIPELINE_TIMEOUT_S,
                        heartbeat_s=PIPELINE_HEARTBEAT_S,
                        log_fn=_log,
                    )
                except Exception as exc:  # noqa: BLE001
                    _log(f"  FAIL: pipeline[{strat_name}] 抛异常: {type(exc).__name__}: {exc}")
                    _teardown_uvicorn(webtrader_uv, "webtrader")
                    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                    main_engine.close()
                    return 3
                if status == "ok":
                    _log(f"  pipeline[{strat_name}] done status=ok elapsed={elapsed:.1f}s")
                elif status in ("empty", "non_trading"):
                    _log(f"  pipeline[{strat_name}] status={status} elapsed={elapsed:.1f}s (允许)")
                else:
                    _log(f"  FAIL: pipeline[{strat_name}] status={status} elapsed={elapsed:.1f}s")
                    _teardown_uvicorn(webtrader_uv, "webtrader")
                    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                    main_engine.close()
                    return 3

            # Phase 5.4 — 实时段每日尾 sleep
            if not is_last and SMOKE_LIVE_SECONDS_PER_DAY > 0:
                _log(f"  [live] sleep {SMOKE_LIVE_SECONDS_PER_DAY}s 让 mlearnweb snapshot loop tick")
                time.sleep(SMOKE_LIVE_SECONDS_PER_DAY)
    else:
        _log("=== Phase 5 — 跳过 (SMOKE_LIVE_DAYS=0) ===")

    # --- Phase 6: 多策略多日断言 ---
    _log("=== Phase 6 — 断言 ===")
    errors: List[str] = []
    out_root = STRATEGY_BASE_SETTING["output_root"]

    # 收集所有跑过的天 (回放段 + 实时段) 用于断言
    # 回放段日期由策略 batch replay 隐式产出, 这里通过扫 OUT_ROOT 对应 strategy 子目录获取
    # 仅 single-day 实时段我们从 live_days 直接拿
    for strat_name, _gw in started:
        # 实时段 single-day 产物: 每天必有 status=ok 且四件套齐
        for day in live_days:
            errors.extend(assert_strategy_day_outputs(
                strat_name, out_root, day, expected_topk=7,
                require_status_ok=False,  # 允许 status=empty (lookback 不足时)
            ))

    if SMOKE_ENABLE_TRADING and live_days:
        # sim 撮合发生在 day+1 09:30, 仅检实时段下的撮合 (回放段下单走的是
        # batch replay 内 in-process apply, 也会写 sim_trades, 但只验实时段更精准)
        for strat_name, gw_name in started:
            errors.extend(assert_orders_for_gateway(
                gw_name, strat_name, list(live_days),
                sim_db_dir=SIM_DB_DIR,
                min_trades_per_day=1,
            ))

    if errors:
        _log(f"FAIL: {len(errors)} 条断言失败:")
        for e in errors:
            _log(f"  - {e}")
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 4

    _log(
        f"PASS: batch replay [..., {replay_end}] + single-day {len(live_days)} day(s) "
        f"x {len(started)} 策略 全部断言通过 [OK]"
    )

    if not SMOKE_KEEP_ALIVE:
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 0

    # --- Phase 7: 常驻 ---
    _log("=== Phase 7 — READY ===")
    _log("回放 + 实时模拟全流程完成, 所有断言通过.")
    _log(f"webtrader REST :{WEBTRADER_HTTP_PORT} | mlearnweb live_main :{MLEARNWEB_PORT}")
    _log("Ctrl+C to exit.")

    stop = {"v": False}

    def _sigint(_s, _f):
        _log("SIGINT received, exiting...")
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

    for strat_name, _gw in started:
        try:
            ml_engine.stop_strategy(strat_name)
        except Exception as exc:  # noqa: BLE001
            _log(f"  stop_strategy({strat_name}) 异常: {exc}")
    _teardown_uvicorn(webtrader_uv, "webtrader")
    _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
    main_engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
