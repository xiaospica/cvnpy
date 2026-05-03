# -*- coding: utf-8 -*-
"""ML 策略多日加速测试脚本 — run_ml_headless 的"测试增强孪生".

## 测什么

run_ml_headless.py 启动后只能在每日 21:00 cron 触发一次推理 + 下单, 无法
对实时模拟阶段的"定时拉数 → 定时推理 → 定时交易"循环做端到端验证.
本脚本基于 run_ml_headless 同一份 GATEWAYS / STRATEGIES / STRATEGY_BASE_SETTING,
把多日加速循环 + 后台 ingest + 心跳轮询 + 多策略多日断言叠加上去.

## 阶段切分 (核心契约)

参数 ``SMOKE_REPLAY_DAYS = N`` (默认 5):

  - 回放段: 各策略 ``replay_start_date`` 起的 **前 N 个交易日**
            (模型上线前的"历史预热")
  - 实时段: 第 N+1 个交易日 → ``today-1`` (取最近已完整收盘交易日,
            不会拉未来数据导致 ingest fail; 对应"上线后每日 cron 推理")
            实时段每日尾插 ``SMOKE_LIVE_SECONDS_PER_DAY`` sleep 让 mlearnweb
            snapshot loop 与 webtrader REST 能采到当日数据.

两段无缝衔接, 同一日序列循环, 仅 phase 标签不同. ``SMOKE_LIVE_DAYS_LIMIT > 0``
可对实时段额外截尾 (从前往后取). 边界:
  - 总可用交易日 ≤ N: 全归回放段, 实时段为空 (告警提示)
  - today 非交易日: live_max = resolve_live_date() 取最近已收盘交易日

## 与 run_ml_headless 等价性

走完全相同的代码路径: ``MLEngine.run_pipeline_now → run_daily_pipeline →
QlibPredictor subprocess``. 仅外围差异:
  - ingest 由 helper 显式触发 (生产是 cron)
  - 派生 webtrader / mlearnweb uvicorn 子进程 (生产由别处启)
  - ``run_pipeline_now(strategy_name, as_of_date=day)`` 显式注入历史日期
    (生产 cron 触发时 as_of_date=None 走 date.today())

不 monkey-patch ``date.today()`` — template.run_daily_pipeline 显式传 as_of_date
后所有下游都从 today=as_of_date 派生, 无需 patch.

## 运行

```
F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 默认: 回放 5 个交易日 + 之后所有可用交易日作实时段
# 缩短: 回放 2 个交易日 + 实时段最多 2 天
SMOKE_REPLAY_DAYS=2 SMOKE_LIVE_DAYS_LIMIT=2 \\
    F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 长回放: 回放 60 个交易日 (lookback 预热) + 实时段不限
SMOKE_REPLAY_DAYS=60 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 关下单 (仅验证流水线产出)
SMOKE_ENABLE_TRADING=0 F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py
```

## 与 smoke_full_pipeline.py 区别

  smoke_full_pipeline:  单策略硬编码 jq41_csi300_2026, 默认 1 天 ingest+推理
  run_ml_headless_smoke: 复用 run_ml_headless 的多策略多 gateway 配置,
                         默认回放 5 天 + 实时 3 天, 多策略串行驱动

两者都用同一份 _pipeline_drivers helper.
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
    build_next_n_trade_days,
    build_trade_days,
    resolve_live_date,
    run_ingest_with_heartbeat,
    wait_pipeline_with_heartbeat,
)


# =====================================================================
# 配置区
# =====================================================================

# --- 1) 阶段切分 ---
# 回放/实时切分语义 (用户原话):
#   - 回放段:  replay_start_date 起的 **前 N 个交易日**       (N = SMOKE_REPLAY_DAYS)
#   - 实时段:  第 N+1 个交易日  →  today-1 (取最近已收盘交易日, 不会拉未来数据)
#
# 与 cron 语义对齐: 回放段是模型上线前的"历史预热", 实时段对应"上线后每日 cron 推理".
# N 控制 "预热期长度", 不可为 0 (没有回放段就退化为纯实时, 无意义).
#
# 边界情况:
#   - replay_start 起的总可用交易日 <= N: 全部归为回放段, 实时段为空 (告警提示)
#   - today 非交易日: 实时段终点取 resolve_live_date() — 最近已完整收盘交易日
#   - SMOKE_LIVE_DAYS_LIMIT > 0: 实时段额外截尾到最多 N 个交易日 (从前往后取)
SMOKE_REPLAY_DAYS: int = int(os.getenv("SMOKE_REPLAY_DAYS", "5"))

# 实时段交易日数上限, 0=不限 (实际上限受 today-1 约束)
SMOKE_LIVE_DAYS_LIMIT: int = int(os.getenv("SMOKE_LIVE_DAYS_LIMIT", "0"))

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

    # --- Phase 2: add + init + start 所有策略 ---
    _log("=== Phase 2 — add_strategy ===")
    valid_gw = {gw["name"] for gw in GATEWAYS}
    started: List[Tuple[str, str]] = []  # [(strategy_name, gateway_name)]

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
            # smoke 强制覆盖: 启用下单 + 拉长 subprocess timeout
            "enable_trading": SMOKE_ENABLE_TRADING,
            "subprocess_timeout_s": PIPELINE_TIMEOUT_S,
        }
        _log(f"  adding {name} → gateway={gw_name} (enable_trading={SMOKE_ENABLE_TRADING})")
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

    # --- Phase 3: 构造日序列 (回放 + 实时无缝) ---
    _log("=== Phase 3 — 构造日序列 ===")
    downloader = tushare_engine._get_tushare_datafeed().downloader

    def _is_trade_date_fn(d: date) -> bool:
        return bool(downloader.is_trade_date(d.strftime("%Y%m%d")))

    today = date.today()
    live_max = resolve_live_date(_is_trade_date_fn)  # 最近已完整收盘交易日 (实时段终点)
    _log(f"  today={today}, live_max={live_max} (最近已收盘交易日 = 实时段终点)")

    # 各策略 replay_start_date 取最早 (双策略起点不同时, 先开始的策略多跑几天)
    replay_starts: List[date] = []
    for strat_def in STRATEGIES:
        rs_str = strat_def["setting_override"].get("replay_start_date")
        if rs_str:
            from datetime import datetime as _dt
            replay_starts.append(_dt.strptime(rs_str, "%Y-%m-%d").date())
    if not replay_starts:
        _log("FAIL: 所有策略都没配 replay_start_date, 无法构造回放段")
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 2
    replay_start = min(replay_starts)
    _log(f"  replay_start={replay_start} (取所有策略 replay_start_date 最小值)")

    # 列出 [replay_start, live_max] 之间所有交易日, 然后按 SMOKE_REPLAY_DAYS 切分
    all_days = build_trade_days(replay_start, live_max, _is_trade_date_fn)
    if not all_days:
        _log(f"FAIL: [{replay_start}, {live_max}] 内无交易日")
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 2

    if SMOKE_REPLAY_DAYS <= 0:
        _log(f"FAIL: SMOKE_REPLAY_DAYS={SMOKE_REPLAY_DAYS} 必须 >= 1")
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 2

    # 切分: 前 N 个交易日为回放, 第 N+1 起为实时段; N 超过总数时全归回放
    n = min(SMOKE_REPLAY_DAYS, len(all_days))
    replay_days = all_days[:n]
    live_days = all_days[n:]
    if SMOKE_LIVE_DAYS_LIMIT > 0 and len(live_days) > SMOKE_LIVE_DAYS_LIMIT:
        live_days = live_days[:SMOKE_LIVE_DAYS_LIMIT]
    if SMOKE_REPLAY_DAYS > len(all_days):
        _log(
            f"  WARN: SMOKE_REPLAY_DAYS={SMOKE_REPLAY_DAYS} > 总可用交易日 {len(all_days)}, "
            f"全部归为回放段, 实时段为空"
        )

    days: List[Tuple[date, str]] = (
        [(d, "replay") for d in replay_days] + [(d, "live") for d in live_days]
    )
    _log(
        f"  回放 {len(replay_days)} 日 [{replay_days[0] if replay_days else '-'} ~ {replay_days[-1] if replay_days else '-'}]"
        f" + 实时 {len(live_days)} 日 [{live_days[0] if live_days else '-'} ~ {live_days[-1] if live_days else '-'}]"
    )
    est_min = (
        len(days) * len(started) * 75 / 60.0  # 每策略每日 ~75s subprocess
        + len(live_days) * SMOKE_LIVE_SECONDS_PER_DAY / 60.0
        + len(days) * (60 if SMOKE_DO_INGEST else 0) / 60.0
    )
    _log(f"  预估总时长: ~{est_min:.0f} 分钟 (subprocess + ingest + live sleep)")

    # --- Phase 4: 统一日推进循环 ---
    _log("=== Phase 4 — 日推进循环 ===")
    for idx, (day, phase) in enumerate(days):
        is_last = idx == len(days) - 1
        day_str = day.strftime("%Y%m%d")
        _log(f"--- Day {idx + 1}/{len(days)} [{phase}] {day} ({day_str}) ---")

        # Phase 4.1 — Ingest
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
                _log(f"  SKIPPED: {day} 非交易日, 跳过本日全流程")
                continue
            stages = result.get("stages_elapsed", {})
            stage_parts = [f"{s}={stages.get(s, 0):.1f}s" for s in
                           ("fetch", "filter", "by_stock", "dump") if s in stages]
            _log(
                f"  ingest OK merged_rows={result.get('merged_rows')} "
                f"filtered_today_rows={result.get('filtered_today_rows')} "
                f"stages=[{' | '.join(stage_parts)}] total={elapsed:.1f}s"
            )

        # Phase 4.2 — 多策略串行 pipeline
        for strat_name, _gw in started:
            try:
                ok, elapsed = wait_pipeline_with_heartbeat(
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
            if ok:
                _log(f"  pipeline[{strat_name}] done elapsed={elapsed:.1f}s")
            else:
                _log(f"  TIMEOUT: pipeline[{strat_name}] 未在 {PIPELINE_TIMEOUT_S}s 内产出新 diagnostics")
                _teardown_uvicorn(webtrader_uv, "webtrader")
                _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
                main_engine.close()
                return 3

        # Phase 4.3 — 实时段每日尾 sleep
        if phase == "live" and not is_last and SMOKE_LIVE_SECONDS_PER_DAY > 0:
            _log(f"  [live] sleep {SMOKE_LIVE_SECONDS_PER_DAY}s 让 mlearnweb snapshot loop tick")
            time.sleep(SMOKE_LIVE_SECONDS_PER_DAY)

    # --- Phase 5: 多策略多日断言 ---
    _log("=== Phase 5 — 断言 ===")
    errors: List[str] = []
    out_root = STRATEGY_BASE_SETTING["output_root"]
    for strat_name, _gw in started:
        for day, _phase in days:
            errors.extend(assert_strategy_day_outputs(
                strat_name, out_root, day, expected_topk=7,
                require_status_ok=False,  # 允许非交易日 / lookback 不足时 empty
            ))

    if SMOKE_ENABLE_TRADING:
        # sim 撮合发生在 day+1 09:30 — 取所有 day 都查 sim_trades
        for strat_name, gw_name in started:
            errors.extend(assert_orders_for_gateway(
                gw_name, strat_name, [d for d, _ in days],
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

    _log(f"PASS: {len(days)} 日 x {len(started)} 策略 全部断言通过 [OK]")

    if not SMOKE_KEEP_ALIVE:
        _teardown_uvicorn(webtrader_uv, "webtrader")
        _teardown_uvicorn(mlearnweb_uv, "mlearnweb")
        main_engine.close()
        return 0

    # --- Phase 6: 常驻 ---
    _log("=== Phase 6 — READY ===")
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
