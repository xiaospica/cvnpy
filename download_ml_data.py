"""Download and build daily ML market data snapshots.

This is the data-only extraction of
``vnpy_ml_strategy/test/smoke_full_pipeline.py``:

1. Load runtime env and Tushare token.
2. Initialize ``TushareProEngine`` with the ``tushare_pro`` datafeed.
3. For ``skip-dump``/``full``, initialize ``MLEngine`` only far enough to read
   the bundle filter config.
4. Run the requested ingest mode for one day or a rolling set of trading days:
   ``fetch-only``, ``skip-dump`` or ``full``.

The script does not start webtrader/mlearnweb and does not run inference.
Default ``--ingest-mode full`` still rewrites ``<VNPY_DATA_ROOT>/qlib_data_bin``.
Use ``--ingest-mode fetch-only`` for non-qlib strategies that only need merged
snapshots for QMT_SIM.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_BUNDLE_DIR = (
    r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/"
    r"f6017411b44c4c7790b63c5766b93964"
)
DEFAULT_INFERENCE_PYTHON = r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe"


def _log(message: str) -> None:
    print(f"[download_ml_data] {message}", flush=True)


def _load_dotenv_file(path: Path) -> None:
    """Small dotenv fallback for the deployment file format used here."""
    if not path.exists():
        return

    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _load_runtime_env() -> None:
    """Load DOTENV_FILE, .env.production, then .env before resolving paths."""
    candidates: list[Path] = []
    dotenv_file = os.getenv("DOTENV_FILE", "").strip()
    if dotenv_file:
        candidates.append(ROOT / dotenv_file)
    candidates.extend([ROOT / ".env.production", ROOT / ".env"])

    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except ModuleNotFoundError:
            _load_dotenv_file(env_path)
        _log(f"loaded env: {env_path}")
        return


def _parse_trade_date(value: str) -> date:
    """Parse ``YYYYMMDD`` or ``YYYY-MM-DD`` into ``date``."""
    text = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"invalid date {value!r}, expected YYYYMMDD or YYYY-MM-DD")


def _load_tushare_token() -> str:
    """Read Tushare token from env first, then ``api.json``."""
    token = (
        os.getenv("TUSHARE_TOKEN")
        or os.getenv("VNPY_DATAFEED_PASSWORD")
        or os.getenv("TUSHARE_PRO_TOKEN")
    )
    if token:
        return token

    api_json = ROOT / "api.json"
    if not api_json.exists():
        raise FileNotFoundError(f"api.json 不存在，且环境变量没有 TUSHARE_TOKEN: {api_json}")
    data = json.loads(api_json.read_text(encoding="utf-8"))
    token = data.get("token") or data.get("password") or data.get("tushare_token")
    if not token:
        raise RuntimeError(f"api.json 里找不到 tushare token: {data.keys()}")
    return str(token)


def _setup_ingest_env(args: argparse.Namespace, token: str) -> dict[str, Path]:
    """Set the same data-path environment used by smoke ingest."""
    if args.data_root:
        os.environ["VNPY_DATA_ROOT"] = str(args.data_root)

    if "ML_INGEST_LOOKBACK_DAYS" not in os.environ or args.lookback_days is not None:
        os.environ["ML_INGEST_LOOKBACK_DAYS"] = str(args.lookback_days or 250)

    os.environ["TUSHARE_TOKEN"] = token
    os.environ["ML_DAILY_INGEST_ENABLED"] = "1"
    os.environ.setdefault("VNPY_DATAFEED_USERNAME", "tushare")
    os.environ["VNPY_DATAFEED_PASSWORD"] = token

    from vnpy_common.data_paths import ml_output_root, vnpy_data_root

    data_root = vnpy_data_root()
    os.environ["VNPY_DATA_ROOT"] = str(data_root)

    defaults = {
        "ML_MERGED_PARQUET_PATH": data_root / "stock_data" / "daily_merged_all_new.parquet",
        "ML_FILTERED_PARQUET_PATH": data_root / "csi300_custom_filtered.parquet",
        "ML_BY_STOCK_CSV_DIR": data_root / "stock_data" / "by_stock",
        "ML_QLIB_DIR": data_root / "qlib_data_bin",
        "ML_SNAPSHOT_DIR": data_root / "snapshots",
        "ML_LIVE_OUTPUT_ROOT": ml_output_root(),
    }
    for key, path in defaults.items():
        os.environ.setdefault(key, str(path))

    os.environ.setdefault(
        "ML_JQ_INDEX_CSV_PATHS",
        json.dumps({"csi300": str(data_root / "jq_index" / "hs300_*.csv")}),
    )
    return {"data_root": data_root, **defaults}


def _resolve_live_date(downloader: Any, explicit: date | None, ready_hour: int) -> date:
    """Return the latest trading day whose daily bar should already exist."""
    if explicit is not None:
        return explicit

    def _is_trade_day(day: date) -> bool:
        try:
            return bool(downloader.is_trade_date(day.strftime("%Y%m%d")))
        except Exception:
            return True

    today = date.today()
    if datetime.now().hour >= ready_hour and _is_trade_day(today):
        return today

    candidate = today - timedelta(days=1)
    for _ in range(10):
        if _is_trade_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return candidate


def _build_days(live_date: date, rolling_days: int, downloader: Any) -> list[date]:
    """Build the trading-day list, inclusive of ``live_date``."""
    if rolling_days <= 0:
        return [live_date]

    days: list[date] = []
    candidates = [live_date - timedelta(days=i) for i in range(rolling_days, -1, -1)]
    for candidate in candidates:
        try:
            if bool(downloader.is_trade_date(candidate.strftime("%Y%m%d"))):
                days.append(candidate)
        except Exception:
            days.append(candidate)
    return days


def _run_ingest_with_heartbeat(
    pipeline: Any,
    day: date,
    *,
    force: bool,
    ingest_mode: str,
    heartbeat_s: float,
) -> dict[str, Any]:
    """Run one ingest in a worker thread and report progress by heartbeat."""
    day_str = day.strftime("%Y%m%d")
    state: dict[str, Any] = {"result": None, "error": None, "done": False}

    def _worker() -> None:
        try:
            if ingest_mode == "fetch-only":
                state["result"] = _run_ingest_fetch_only(pipeline, day_str, force=force)
            elif ingest_mode == "skip-dump":
                state["result"] = _run_ingest_without_dump(pipeline, day_str, force=force)
            else:
                state["result"] = pipeline.ingest_today(day_str, force=force)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            state["done"] = True

    thread = threading.Thread(target=_worker, daemon=False, name=f"ingest-{day_str}")
    start = time.monotonic()
    last_heartbeat = start
    thread.start()

    while not state["done"]:
        time.sleep(0.5)
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_s:
            _log(f"  ... ingest[{day_str}] running ({now - start:.0f}s elapsed)")
            last_heartbeat = now

    thread.join(timeout=5)
    if state["error"] is not None:
        raise state["error"]
    result = state["result"]
    if not isinstance(result, dict):
        raise RuntimeError(f"ingest[{ingest_mode}]({day_str}) returned non-dict result: {result!r}")
    return result


def _run_ingest_fetch_only(pipeline: Any, trade_date: str, *, force: bool) -> dict[str, Any]:
    """Run fetch only: merged snapshots plus QMT_SIM stock+fund snapshots."""
    lock = getattr(pipeline, "_lock", None)
    if lock is None:
        return _run_ingest_fetch_only_locked(pipeline, trade_date, force=force)
    with lock:
        return _run_ingest_fetch_only_locked(pipeline, trade_date, force=force)


def _run_ingest_fetch_only_locked(pipeline: Any, trade_date: str, *, force: bool) -> dict[str, Any]:
    """Implementation for ``_run_ingest_fetch_only`` after lock acquisition."""
    start = time.time()
    if not pipeline._is_trade_date(trade_date):
        return {
            "trade_date": trade_date,
            "skipped": True,
            "duration_s": 0.0,
            "stages_done": [],
            "ingest_mode": "fetch-only",
        }

    pipeline._cleanup_old_snapshots()
    merged_df = pipeline._stage_fetch(trade_date, force=force)
    result = {
        "trade_date": trade_date,
        "merged_rows": int(len(merged_df)),
        "filtered_today_rows": None,
        "qlib_calendar_last_date": None,
        "duration_s": time.time() - start,
        "stages_done": ["fetch"],
        "skipped": False,
        "ingest_mode": "fetch-only",
        "dump_skipped": True,
    }

    append_audit_log = getattr(pipeline, "_append_audit_log", None)
    if callable(append_audit_log):
        append_audit_log({**result, "status": "ok_fetch_only"})
    return result


def _run_ingest_without_dump(pipeline: Any, trade_date: str, *, force: bool) -> dict[str, Any]:
    """Run fetch/filter/by_stock only, matching DailyIngestPipeline before dump.

    This intentionally uses the pipeline's private stage methods because the
    production ``ingest_today`` contract is qlib-oriented and always publishes
    ``qlib_data_bin``. The standalone script needs a narrower data-only mode
    for non-qlib consumers while keeping production behavior unchanged.
    """
    lock = getattr(pipeline, "_lock", None)
    if lock is None:
        return _run_ingest_without_dump_locked(pipeline, trade_date, force=force)
    with lock:
        return _run_ingest_without_dump_locked(pipeline, trade_date, force=force)


def _run_ingest_without_dump_locked(pipeline: Any, trade_date: str, *, force: bool) -> dict[str, Any]:
    """Implementation for ``_run_ingest_without_dump`` after lock acquisition."""
    start = time.time()
    stages_done: list[str] = []
    if not pipeline._is_trade_date(trade_date):
        return {"trade_date": trade_date, "skipped": True, "duration_s": 0.0, "stages_done": []}

    pipeline._cleanup_old_snapshots()
    merged_df = pipeline._stage_fetch(trade_date, force=force)
    stages_done.append("fetch")
    filtered_today_rows = pipeline._stage_filter(merged_df, trade_date, force=force)
    stages_done.append("filter")
    pipeline._stage_by_stock(merged_df, trade_date)
    stages_done.append("by_stock")

    result = {
        "trade_date": trade_date,
        "merged_rows": int(len(merged_df)),
        "filtered_today_rows": filtered_today_rows,
        "qlib_calendar_last_date": None,
        "duration_s": time.time() - start,
        "stages_done": stages_done,
        "skipped": False,
        "ingest_mode": "skip-dump",
        "dump_skipped": True,
    }

    emit_event = getattr(pipeline, "_emit_event", None)
    append_audit_log = getattr(pipeline, "_append_audit_log", None)
    if callable(emit_event):
        emit_event("EVENT_DAILY_INGEST_OK", result)
    if callable(append_audit_log):
        append_audit_log({**result, "status": "ok_skip_dump"})
    return result


def _init_engines(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """Initialize Tushare and ML engines and inject filter specs."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.setting import SETTINGS
    from vnpy_tushare_pro import TushareProApp
    from vnpy_tushare_pro.engine import APP_NAME as TUSHARE_APP

    SETTINGS["datafeed.name"] = "tushare_pro"
    SETTINGS["datafeed.username"] = "tushare"
    SETTINGS["datafeed.password"] = os.environ["VNPY_DATAFEED_PASSWORD"]

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(TushareProApp)
    if args.ingest_mode != "fetch-only":
        from vnpy_ml_strategy import APP_NAME as ML_APP, MLStrategyApp

        main_engine.add_app(MLStrategyApp)

    tushare_engine = main_engine.get_engine(TUSHARE_APP)
    tushare_engine.init_engine()
    ts_datafeed = tushare_engine._get_tushare_datafeed()
    ts_pipeline = getattr(ts_datafeed, "daily_ingest_pipeline", None)
    downloader = getattr(ts_datafeed, "downloader", None)
    if ts_pipeline is None:
        raise RuntimeError("DailyIngestPipeline 未启用，请检查 ML_DAILY_INGEST_ENABLED/VNPY_DATA_ROOT")
    if downloader is None:
        raise RuntimeError(
            "vnpy datafeed 未正确加载为 TushareDatafeedPro，"
            f"当前类型={type(ts_datafeed).__module__}.{type(ts_datafeed).__name__}"
        )
    _log(
        "TushareProEngine ready: "
        f"datafeed={type(ts_datafeed).__module__}.{type(ts_datafeed).__name__}"
    )

    if args.ingest_mode == "fetch-only":
        _log("fetch-only mode: skip ML engine and filter config injection")
        return main_engine, ts_pipeline, downloader

    ml_engine = main_engine.get_engine(ML_APP)
    ml_engine.init_engine()
    bundle_dir = str(Path(args.bundle_dir).expanduser())
    strategy = ml_engine.add_strategy(
        "QlibMLStrategy",
        args.strategy_name,
        {
            "bundle_dir": bundle_dir,
            "inference_python": args.inference_python,
            "provider_uri": os.environ["ML_QLIB_DIR"],
            "output_root": os.environ["ML_LIVE_OUTPUT_ROOT"],
            "gateway": args.gateway,
            "trigger_time": "21:00",
            "topk": args.topk,
            "lookback_days": 60,
            "subprocess_timeout_s": 300,
            "enable_trading": False,
            "enable_replay": False,
        },
    )
    if not ml_engine.init_strategy(args.strategy_name):
        raise RuntimeError(f"ML strategy init failed: {args.strategy_name} bundle={bundle_dir}")

    specs = ml_engine.list_active_filter_configs()
    if not specs:
        raise RuntimeError("ml_engine.list_active_filter_configs() 返回空，无法注入过滤配置")
    ts_pipeline.set_filter_chain_specs(specs)
    _log(f"filter_chain_specs injected: {list(specs.keys())}")
    _log(f"strategy initialized for filter config only: {strategy.strategy_name}")
    return main_engine, ts_pipeline, downloader


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run DailyIngestPipeline data download/build without starting web services.",
    )
    parser.add_argument("--date", type=_parse_trade_date, help="目标交易日，YYYYMMDD 或 YYYY-MM-DD")
    parser.add_argument(
        "--rolling-days",
        type=int,
        default=int(os.getenv("SIMULATE_ROLLING_DAYS", "0")),
        help="从目标日往前回溯 N 个自然日，只保留交易日逐日 ingest",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="显式设置 VNPY_DATA_ROOT；默认读取 .env/.env.production/环境变量",
    )
    parser.add_argument(
        "--bundle-dir",
        default=os.getenv("ML_DOWNLOAD_BUNDLE_DIR") or os.getenv("BUNDLE_DIR") or DEFAULT_BUNDLE_DIR,
        help="用于读取 filter_config.json 的 qlib bundle 目录",
    )
    parser.add_argument(
        "--strategy-name",
        default=os.getenv("ML_DOWNLOAD_STRATEGY_NAME", "download_ml_data_filter_probe"),
        help="临时策略名，仅用于让 MLEngine 注册 bundle filter_config",
    )
    parser.add_argument("--gateway", default="", help="临时策略 gateway 参数；默认留空，避免连接/查询 gateway")
    parser.add_argument("--topk", type=int, default=7, help="临时策略 topk 参数")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.getenv("ML_INGEST_LOOKBACK_DAYS", "250")),
        help="DailyIngestPipeline snapshot 窗口自然日数",
    )
    parser.add_argument("--ready-hour", type=int, default=20, help="未指定 --date 时，当日 bar 可用小时")
    parser.add_argument("--heartbeat-s", type=float, default=10.0, help="ingest 心跳日志间隔秒数")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的当日 merged/filter 快照")
    parser.add_argument(
        "--ingest-mode",
        choices=["fetch-only", "skip-dump", "full"],
        default=os.getenv("ML_DOWNLOAD_INGEST_MODE", "full").strip().lower().replace("_", "-"),
        help=(
            "fetch-only=只跑 fetch 并生成 merged/QMT_SIM 快照；"
            "skip-dump=跑 fetch/filter/by_stock 但跳过 qlib dump；"
            "full=完整 ingest_today"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="只验证环境、filter 注入和日期解析，不执行下载")
    parser.add_argument(
        "--inference-python",
        default=os.getenv("INFERENCE_PYTHON", DEFAULT_INFERENCE_PYTHON),
        help="仅用于临时策略参数，本脚本不会启动推理子进程",
    )
    return parser


def main() -> int:
    _load_runtime_env()
    args = _build_arg_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    token = _load_tushare_token()
    paths = _setup_ingest_env(args, token)
    _log(f"VNPY_DATA_ROOT={paths['data_root']}")
    _log(f"ML_QLIB_DIR={os.environ['ML_QLIB_DIR']}")
    _log(f"ML_SNAPSHOT_DIR={os.environ['ML_SNAPSHOT_DIR']}")
    _log(f"bundle_dir={args.bundle_dir}")

    main_engine = None
    try:
        main_engine, pipeline, downloader = _init_engines(args)
        live_date = _resolve_live_date(downloader, args.date, args.ready_hour)
        days = _build_days(live_date, args.rolling_days, downloader)
        if not days:
            raise RuntimeError(f"rolling_days={args.rolling_days} 范围内没有交易日")

        if args.date:
            _log(f"target_date={live_date} (from --date)")
        else:
            _log(f"target_date={live_date} (auto resolved, ready_hour={args.ready_hour})")
        _log(
            f"ingest_days={[d.isoformat() for d in days]} "
            f"force={args.force} ingest_mode={args.ingest_mode}"
        )
        if args.dry_run:
            _log("dry-run enabled; skip ingest_today")
            return 0

        for index, day in enumerate(days, start=1):
            day_str = day.strftime("%Y%m%d")
            _log(f"=== Day {index}/{len(days)} {day.isoformat()} ({day_str}) ===")
            started = time.monotonic()
            result = _run_ingest_with_heartbeat(
                pipeline,
                day,
                force=args.force,
                ingest_mode=args.ingest_mode,
                heartbeat_s=args.heartbeat_s,
            )
            elapsed = time.monotonic() - started
            if result.get("skipped"):
                _log(f"  SKIPPED non-trading day: {day_str}")
                continue
            stages = ",".join(result.get("stages_done", []))
            _log(
                "  OK "
                f"merged_rows={result.get('merged_rows')} "
                f"filtered_today_rows={result.get('filtered_today_rows')} "
                f"qlib_calendar_last={result.get('qlib_calendar_last_date')} "
                f"stages=[{stages}] total={elapsed:.1f}s"
            )

        _log("all requested ingest days completed")
        return 0
    except KeyboardInterrupt:
        _log("interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        _log(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if main_engine is not None:
            try:
                main_engine.close()
            except Exception as exc:  # noqa: BLE001
                _log(f"WARN: main_engine.close failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
