# -*- coding: utf-8 -*-
"""SignalStrategyPlus 聚宽信号双轨启动器。

本脚本是 SignalStrategyPlus 的正式近实盘启动入口，目标策略为
``RedisLiveSimTestStrategy``，信号事实源为 MySQL v2
``trade_signal_events`` journal。

模式语义
--------
``--mode v1`` / ``--mode single``
    单 QMT_SIM gateway + 单策略。用于无 miniQMT 风险地回放 MySQL 信号 journal，
    验证本地模拟撮合、权益 journal 和 mlearnweb 展示。

``--mode v2``
    名为 ``QMT`` 的 FakeQmtGateway + 一个 QMT_SIM shadow 策略。两条腿都使用
    simulator 撮合，不会向券商发单；用于验证 live 命名槽位、gateway 路由、
    账户 DB 隔离、shadow 信号镜像和前端展示。

``--mode v3``
    真实 ``QmtGateway``（名称固定为 ``QMT``）+ 一个 QMT_SIM shadow 策略。真实
    source 腿只允许走实时链路，绝不回放历史 MySQL 信号。默认情况下 source 腿
    也不会轮询/消费实时信号，因此不能向券商发单；只有显式传入
    ``--allow-live-orders`` 后，才会启用真实 source 腿的信号轮询和下单路径。
    shadow 腿仍可镜像并回放 source 历史信号，用于观察策略行为。

关键入参
--------
``--config``
    运行配置文件。默认读取 ``SIGNAL_DUAL_TRACK_CONFIG``，未设置时读取
    ``<VNPY_DATA_ROOT>/config/signal_dual_track.json``。正式启动不再默认读取
    ``vnpy_signal_strategy_plus/test/*.json`` 历史测试配置。
``--allow-live-orders``
    仅 v3 有效。显式武装真实 QMT source 腿；不传则真实腿不消费信号、不下单。

``--live-signal-cutoff startup|today``
    仅 v3 且 ``--allow-live-orders`` 时有效。默认 ``startup``，只消费启动后新信号，
    避免重启后重复消费当天旧信号；``today`` 允许消费今天 00:00 后未消费信号。

``--cleanup-scope default|none|shadow|all-sim``
    默认 v3=``shadow``，只清 shadow sim 状态和 shadow checkpoint；v1/v2=``all-sim``。
    v3 默认和 ``all-sim`` 都不会删除真实 source 策略的消费 checkpoint。

``--shadow-replay historical|none``
    是否允许 shadow QMT_SIM 腿回放历史镜像信号。默认 ``historical``，便于观察。

安全默认值
----------
* v3 source cleanup 不删除 source 策略消费 checkpoint。
* v3 source cutoff 默认 ``startup``，避免重启后重复消费当天旧信号。
* v3 shadow cleanup 只触碰 shadow sim 状态、shadow 信号和 shadow checkpoint。
* ``pct`` 语义固定为：本次交易金额 / 组合总资产。

典型使用示例
------------
    # v1: 单 QMT_SIM 回放 harvester_micro_cap_1
    F:/Program_Home/vnpy/python.exe -u run_signal_dual_track.py --mode v1 --source-stg harvester_micro_cap_1

    # v2: FakeQMT source + QMT_SIM shadow，无真实下单风险
    F:/Program_Home/vnpy/python.exe -u run_signal_dual_track.py --mode v2 --source-stg harvester_micro_cap_1 --runner-id local_pc

    # v3: 真实 QMT source + QMT_SIM shadow；默认只连接和观察，不消费 source 信号、不下单
    F:/Program_Home/vnpy/python.exe -u run_signal_dual_track.py --mode v3 --qmt-account YOUR_PAPER_ACCOUNT --source-stg harvester_micro_cap_1 --runner-id tencent_qmt_01

    # v3: 显式武装真实 source 腿，只消费启动后新信号并可能向 QMT 发单
    F:/Program_Home/vnpy/python.exe -u run_signal_dual_track.py --mode v3 --qmt-account YOUR_PAPER_ACCOUNT --source-stg harvester_micro_cap_1 --runner-id tencent_qmt_01 --allow-live-orders --live-signal-cutoff startup
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Type
from urllib.parse import quote_plus


os.environ.setdefault("VNPY_DOCK_BACKEND", "ads")

_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is available in the vnpy env.
    load_dotenv = None

_DOTENV_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "mbcs")


def _load_dotenv_compat(path: Path, *, override: bool = False) -> bool:
    """Load .env files saved as UTF-8 or Windows ANSI/GBK.

    python-dotenv defaults to UTF-8. On Windows servers, operators may edit
    .env.production with legacy tools that save Chinese paths as ANSI/GBK.
    Trying strict UTF-8 first keeps the preferred format intact, while the
    GBK/mbcs fallbacks prevent deployment scripts from crashing on existing
    production files.
    """
    if load_dotenv is None or not path.exists():
        return False

    last_error: Exception | None = None
    for encoding in _DOTENV_ENCODINGS:
        try:
            return bool(load_dotenv(path, override=override, encoding=encoding))
        except (LookupError, UnicodeDecodeError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return False


if load_dotenv is not None:
    _DOTENV_FILE = os.getenv("DOTENV_FILE")
    if _DOTENV_FILE and (_HERE / _DOTENV_FILE).exists():
        _load_dotenv_compat(_HERE / _DOTENV_FILE, override=False)
    elif (_HERE / ".env.production").exists():
        _load_dotenv_compat(_HERE / ".env.production", override=False)
    elif (_HERE / ".env").exists():
        _load_dotenv_compat(_HERE / ".env", override=False)

from vnpy_common.data_paths import (  # noqa: E402
    config_dir,
    ensure_vnpy_data_env,
    merged_snapshots_dir,
    merged_stock_fund_snapshots_dir,
    state_dir,
    strategy_equity_journal_db_path,
    vnpy_data_root,
)

ensure_vnpy_data_env()


SIGNAL_DUAL_TRACK_CONFIG_ENV = "SIGNAL_DUAL_TRACK_CONFIG"
SIGNAL_RUNNER_ID_ENV = "SIGNAL_RUNNER_ID"
DEFAULT_SETTING_FILENAME = "signal_dual_track.json"
WEBTRADER_HTTP_PORT = 8001
MODE_ALIASES = {"single": "v1", "v1": "v1", "v2": "v2", "v3": "v3"}
MAX_STG_NAME_LEN = 64
RUNNER_ID_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_]+")
DEFAULT_MYSQL_CONNECT_TIMEOUT = 10
DEFAULT_MYSQL_READ_TIMEOUT = 10
DEFAULT_MYSQL_WRITE_TIMEOUT = 10
DAILY_DATA_INGEST_MODES = {"fetch-only", "skip-dump", "full"}


def _default_setting_path() -> Path:
    """Resolve the production dual-track config path.

    Runtime config belongs under VNPY_DATA_ROOT/config by default. The
    historical vnpy_signal_strategy_plus/test/*.json files are test fixtures,
    not production startup defaults.
    """
    explicit = os.getenv(SIGNAL_DUAL_TRACK_CONFIG_ENV, "").strip()
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser()
    return config_dir() / DEFAULT_SETTING_FILENAME


def _load_json(path: Path) -> Dict[str, Any]:
    """Load a UTF-8/UTF-8-SIG JSON file."""
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        lines = text.splitlines()
        start = max(1, exc.lineno - 2)
        end = min(len(lines), exc.lineno + 2)
        context = "\n".join(
            f"{line_no:>4}: {lines[line_no - 1]}"
            for line_no in range(start, end + 1)
        )
        hint = ""
        if "trailing comma" in exc.msg.lower():
            hint = " Hint: remove the comma before the closing } or ]."
        raise ValueError(
            f"Invalid JSON config: {path}\n"
            f"{exc.msg} at line {exc.lineno}, column {exc.colno}.{hint}\n"
            f"{context}"
        ) from exc


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce config-style booleans without treating arbitrary strings as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _load_tushare_token() -> str:
    """Resolve the Tushare token for optional daily data ingest."""
    token = (
        os.getenv("VNPY_DATAFEED_PASSWORD")
        or os.getenv("TUSHARE_TOKEN")
        or os.getenv("TUSHARE_PRO_TOKEN")
    )
    if token:
        return token.strip()

    api_json = _HERE / "api.json"
    if not api_json.exists():
        raise FileNotFoundError(
            "daily data ingest enabled, but no Tushare token was found in "
            "VNPY_DATAFEED_PASSWORD/TUSHARE_TOKEN/TUSHARE_PRO_TOKEN or api.json"
        )
    data = json.loads(api_json.read_text(encoding="utf-8"))
    token = data.get("token") or data.get("password") or data.get("tushare_token")
    if not token:
        raise RuntimeError(f"api.json does not contain a Tushare token: keys={list(data.keys())}")
    return str(token).strip()


def _resolve_daily_data_ingest_config(
    args: argparse.Namespace,
    setting: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve optional Tushare scheduled download config for this runner."""
    raw_cfg = setting.get("daily_data_ingest", {}) or {}
    enabled = bool(args.enable_daily_data_ingest or _coerce_bool(raw_cfg.get("enable"), False))
    mode = str(args.daily_data_mode or raw_cfg.get("mode") or "fetch-only").strip().lower().replace("_", "-")
    if mode not in DAILY_DATA_INGEST_MODES:
        raise ValueError(
            f"daily_data_ingest.mode must be one of {sorted(DAILY_DATA_INGEST_MODES)}, got {mode!r}"
        )
    time_str = str(args.daily_data_time or raw_cfg.get("time") or "20:00").strip() or "20:00"
    return {
        "enable": enabled,
        "mode": mode,
        "env_mode": mode.replace("-", "_"),
        "time": time_str,
    }


def _configure_daily_data_ingest(config: Dict[str, Any]) -> None:
    """Prepare vn.py SETTINGS/env before TushareProApp constructs its datafeed."""
    if not config.get("enable"):
        return

    from vnpy.trader.setting import SETTINGS

    token = _load_tushare_token()
    os.environ["TUSHARE_TOKEN"] = token
    os.environ.setdefault("VNPY_DATAFEED_USERNAME", "tushare")
    os.environ["VNPY_DATAFEED_PASSWORD"] = token
    os.environ["ML_DAILY_INGEST_ENABLED"] = "1"
    os.environ["ML_DAILY_INGEST_MODE"] = str(config["env_mode"])
    os.environ["ML_DAILY_INGEST_TIME"] = str(config["time"])

    SETTINGS["datafeed.name"] = "tushare_pro"
    SETTINGS["datafeed.username"] = os.environ["VNPY_DATAFEED_USERNAME"]
    SETTINGS["datafeed.password"] = token


def _parse_day(value: object) -> date | None:
    """Parse an optional YYYY-MM-DD style day."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return datetime.fromisoformat(text).date()


def _resolve_calendar_path(setting: Dict[str, Any]) -> str | None:
    """Resolve the shared calendar path for replay day decisions."""
    from vnpy_common.trade_calendar import normalize_calendar_path

    replay_cfg = setting.get("replay", {}) or {}
    raw = (
        replay_cfg.get("calendar_path")
        or setting.get("calendar_path")
        or replay_cfg.get("calendar_provider_uri")
        or setting.get("calendar_provider_uri")
    )
    if raw in (None, ""):
        return None
    return str(normalize_calendar_path(_expand_config_path(raw)))


def _resolve_calendar_provider_uri(setting: Dict[str, Any]) -> str | None:
    """Backward-compatible alias for older tests and callers."""
    return _resolve_calendar_path(setting)


def _latest_completed_trade_day(setting: Dict[str, Any]) -> date | None:
    """Return latest completed trade day using the shared A-share calendar."""
    from vnpy_common.trade_calendar import StaleCalendarError, make_calendar

    now = datetime.now()
    today = now.date()
    calendar = make_calendar(_resolve_calendar_path(setting))

    if now.time() >= datetime_time(hour=15):
        try:
            if calendar.is_trade_day(today):
                return today
        except StaleCalendarError as exc:
            print(f"[config] calendar stale for today, use previous trade day: {exc}")
        except Exception as exc:
            print(f"[config] calendar check failed, use previous trade day: {exc}")

    prev_trade_day = getattr(calendar, "prev_trade_day", None)
    if callable(prev_trade_day):
        lookup_day = today + timedelta(days=1) if now.time() >= datetime_time(hour=15) else today
        try:
            return prev_trade_day(lookup_day)
        except Exception as exc:
            print(f"[config] prev_trade_day failed, fallback to weekday: {exc}")

    cursor = today
    for _ in range(14):
        if cursor.weekday() < 5:
            return cursor
        cursor -= timedelta(days=1)
    return None


def _normalize_mode(mode: str) -> str:
    """Normalize public mode aliases to the internal mode name."""
    try:
        return MODE_ALIASES[str(mode).strip().lower()]
    except KeyError as exc:
        raise ValueError("mode must be one of: v1/single, v2, v3") from exc


def _default_cleanup_scope(mode: str) -> str:
    """Return the conservative cleanup scope for a runner mode."""
    return "shadow" if _normalize_mode(mode) == "v3" else "all-sim"


def _sanitize_runner_id(value: object) -> str:
    """Return a stable stg suffix from CLI/env/config input."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = RUNNER_ID_INVALID_CHARS.sub("_", text).strip("_").lower()
    while "__" in text:
        text = text.replace("__", "_")
    return text


def _resolve_runner_id(cli_value: str, setting: Dict[str, Any]) -> str:
    """Resolve the deployment-local runner id used to isolate shadow stg."""
    dual_track_cfg = setting.get("dual_track", {}) or {}
    for raw in (
        cli_value,
        os.getenv(SIGNAL_RUNNER_ID_ENV, ""),
        setting.get("runner_id", ""),
        dual_track_cfg.get("runner_id", ""),
    ):
        runner_id = _sanitize_runner_id(raw)
        if runner_id:
            return runner_id
    return ""


def _validate_stg_name(value: str, *, field: str) -> str:
    """Keep generated strategy/signal names inside the MySQL varchar budget."""
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{field} is required")
    if len(name) > MAX_STG_NAME_LEN:
        raise ValueError(
            f"{field} is too long: {len(name)} > {MAX_STG_NAME_LEN}: {name!r}"
        )
    return name


def _resolve_shadow_stg(
    mode: str,
    source_stg: str,
    requested_shadow_stg: str,
    runner_id: str,
    *,
    allow_shared_shadow_stg: bool = False,
) -> str:
    """Resolve a mirror stg that cannot be shared accidentally by two runners."""
    source_stg = _validate_stg_name(source_stg, field="source_stg")
    shared_shadow_stg = f"{source_stg}_shadow"
    mode = _normalize_mode(mode)

    if mode == "v1":
        return _validate_stg_name(
            requested_shadow_stg or shared_shadow_stg,
            field="shadow_stg",
        )

    if requested_shadow_stg:
        shadow_stg = _validate_stg_name(requested_shadow_stg, field="shadow_stg")
        if shadow_stg == shared_shadow_stg and not allow_shared_shadow_stg:
            raise ValueError(
                f"Refuse shared shadow stg {shadow_stg!r}. Use --runner-id so the "
                f"default becomes {source_stg}_shadow_<runner_id>, pass a unique "
                "--shadow-stg, or use --allow-shared-shadow-stg only for an isolated "
                "one-off test."
            )
        return shadow_stg

    if not runner_id:
        raise ValueError(
            f"--runner-id (or {SIGNAL_RUNNER_ID_ENV}/config runner_id) is required "
            f"for mode {mode} so the shadow mirror stg is isolated per runner."
        )
    return _validate_stg_name(
        f"{source_stg}_shadow_{runner_id}",
        field="shadow_stg",
    )


def _resolve_live_signal_cutoff(mode: str, policy: str) -> Optional[datetime]:
    """Resolve the lower bound for v3 live source signal consumption."""
    if _normalize_mode(mode) != "v3":
        return None
    now = datetime.now()
    if policy == "startup":
        return now
    if policy == "today":
        return datetime.combine(now.date(), datetime_time.min)
    raise ValueError("live signal cutoff must be startup or today")


def _expand_config_path(value: object) -> str:
    ensure_vnpy_data_env()
    return os.path.expandvars(str(value)).strip()


def _expand_paths_in_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_paths_in_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_paths_in_obj(v) for v in value]
    if isinstance(value, str):
        return _expand_config_path(value)
    return value


def _resolve_sim_state_dir(setting: Dict[str, Any]) -> Path:
    """Return the SQLite state directory used by the configured sim gateways."""
    sim_cfg = setting.get("sim", {}) or {}
    raw = sim_cfg.get("db_dir") or (sim_cfg.get("connect_setting", {}) or {}).get("持久化目录")
    return Path(_expand_config_path(raw)) if raw else state_dir()


def _strategy_equity_journal_db() -> Path:
    return strategy_equity_journal_db_path()


def _sim_setting(setting: Dict[str, Any], gateway_name: str) -> Dict[str, Any]:
    """Build per-gateway QMT_SIM/FakeQMT connect setting."""
    sim_cfg = setting.get("sim", {}) or {}
    connect_setting = _expand_paths_in_obj(dict(sim_cfg.get("connect_setting", {}) or {}))
    connect_setting.setdefault("模拟资金", float(setting.get("initial_capital", 1_000_000.0)))
    connect_setting.setdefault("行情源", "merged_parquet")
    sim_state_dir = _resolve_sim_state_dir(setting)
    connect_setting.setdefault("merged_parquet_merged_root", str(merged_stock_fund_snapshots_dir()))
    connect_setting.setdefault("merged_parquet_fallback_roots", str(merged_snapshots_dir()))
    connect_setting.setdefault("merged_parquet_reference_kind", "today_open")
    connect_setting.setdefault("merged_parquet_fallback_days", 10)
    connect_setting.setdefault("启用持久化", "是")
    connect_setting.setdefault("持久化目录", str(sim_state_dir))

    # 多 gateway 必须使用独立 account_id，否则持久化会写进同一个 sim_*.db。
    connect_setting["账户"] = gateway_name
    return connect_setting


def _qmt_live_setting(qmt_account: str) -> Dict[str, Any]:
    """Build real QMT gateway connect setting."""
    return {
        "交易账号": qmt_account,
        "mini路径": os.getenv(
            "QMT_CLIENT_PATH",
            r"E:/迅投极速交易终端 睿智融科版/userdata_mini",
        ),
    }


def _build_config(
    mode: str,
    setting: Dict[str, Any],
    source_stg: str,
    shadow_stg: str,
    qmt_account: str = "",
    *,
    runner_id: str = "",
    shadow_replay: str = "historical",
) -> Dict[str, Any]:
    """Return gateway/strategy config for the requested mode."""
    mode = _normalize_mode(mode)
    source_runtime = {
        "signal_source_stg": source_stg,
        "application_scope_suffix": runner_id,
    }
    shadow_runtime = {
        "role": "shadow-sim",
        "replay_enabled": shadow_replay == "historical",
        "live_orders_enabled": True,
        "signal_source_stg": shadow_stg,
        "application_scope_suffix": runner_id,
    }
    if mode == "v1":
        gateway_name = str(
            (setting.get("sim", {}) or {}).get("gateway_name")
            or (setting.get("sim", {}) or {}).get("account_id")
            or "QMT_SIM"
        )
        return {
            "label": "single QMT_SIM baseline",
            "mirror": False,
            "GATEWAYS": [
                {"kind": "sim", "name": gateway_name, "setting": _sim_setting(setting, gateway_name)},
            ],
            "STRATEGIES": [
                {
                    "class_name": "RedisLiveSimSingle",
                    "strategy_name": source_stg,
                    "gateway_name": gateway_name,
                    "runtime": {
                        **source_runtime,
                        "role": "single-sim",
                        "replay_enabled": True,
                        "live_orders_enabled": True,
                    },
                },
            ],
        }

    shadow_gateway = "QMT_SIM_redis_shadow"
    if mode == "v2":
        return {
            "label": "V2 FakeQmt(QMT) + QMT_SIM shadow",
            "mirror": True,
            "GATEWAYS": [
                {"kind": "fake_live", "name": "QMT", "setting": _sim_setting(setting, "QMT")},
                {"kind": "sim", "name": shadow_gateway, "setting": _sim_setting(setting, shadow_gateway)},
            ],
            "STRATEGIES": [
                {
                    "class_name": "RedisLiveSimV2Live",
                    "strategy_name": source_stg,
                    "gateway_name": "QMT",
                    "runtime": {
                        **source_runtime,
                        "role": "source-fake-live",
                        "replay_enabled": True,
                        "live_orders_enabled": True,
                    },
                },
                {
                    "class_name": "RedisLiveSimV2Shadow",
                    "strategy_name": shadow_stg,
                    "gateway_name": shadow_gateway,
                    "runtime": dict(shadow_runtime),
                },
            ],
        }

    if mode == "v3":
        if not qmt_account:
            raise ValueError("--mode v3 需要 --qmt-account 参数")
        return {
            "label": f"V3 real QmtGateway(QMT account={qmt_account}) + QMT_SIM shadow",
            "mirror": True,
            "GATEWAYS": [
                {"kind": "live", "name": "QMT", "setting": _qmt_live_setting(qmt_account)},
                {"kind": "sim", "name": shadow_gateway, "setting": _sim_setting(setting, shadow_gateway)},
            ],
            "STRATEGIES": [
                {
                    "class_name": "RedisLiveSimV3Live",
                    "strategy_name": source_stg,
                    "gateway_name": "QMT",
                    "runtime": {
                        **source_runtime,
                        "role": "source-live",
                        "replay_enabled": False,
                        "live_orders_enabled": False,
                    },
                },
                {
                    "class_name": "RedisLiveSimV3Shadow",
                    "strategy_name": shadow_stg,
                    "gateway_name": shadow_gateway,
                    "runtime": dict(shadow_runtime),
                },
            ],
        }

    raise ValueError(f"unknown mode {mode!r}")


def _apply_live_runtime_options(
    strategies: List[Dict[str, Any]],
    *,
    mode: str,
    allow_live_orders: bool,
    live_signal_cutoff_dt: Optional[datetime],
) -> None:
    """Apply CLI safety options to the real v3 source leg."""
    if _normalize_mode(mode) != "v3":
        return
    for strategy in strategies:
        runtime = strategy.setdefault("runtime", {})
        if runtime.get("role") != "source-live":
            continue
        runtime["replay_enabled"] = False
        runtime["live_orders_enabled"] = bool(allow_live_orders)
        runtime["live_signal_cutoff_dt"] = live_signal_cutoff_dt


def _strategy_names_for_cleanup(
    strategies: List[Dict[str, Any]],
    sim_gateway_names: Iterable[str],
    cleanup_scope: str,
) -> List[str]:
    """Resolve which strategies are allowed to have local state reset."""
    if cleanup_scope == "none":
        return []
    if cleanup_scope == "shadow":
        shadow_names = [
            str(s["strategy_name"])
            for s in strategies
            if (s.get("runtime") or {}).get("role") == "shadow-sim"
        ]
        if shadow_names:
            return shadow_names
    sim_gateways = set(sim_gateway_names)
    return [
        str(s["strategy_name"])
        for s in strategies
        if str(s.get("gateway_name")) in sim_gateways
    ]


def _validate_config(gateways: List[Dict[str, Any]], strategies: List[Dict[str, Any]]) -> None:
    """Hard-check names before starting the engine."""
    from vnpy_common.naming import validate_gateway_name

    live_count = sum(1 for gw in gateways if gw["kind"] in {"live", "fake_live"})
    if live_count > 1:
        raise ValueError("miniqmt 单进程单账户约束只允许一个 live/fake_live gateway")

    gw_names = set()
    for gw in gateways:
        kind = gw["kind"]
        if kind not in {"sim", "fake_live", "live"}:
            raise ValueError(f"非法 gateway kind={kind!r}")
        expected = "live" if kind in {"fake_live", "live"} else "sim"
        validate_gateway_name(gw["name"], expected_class=expected)
        if kind == "live":
            live_setting = gw.get("setting") or {}
            missing = [
                key
                for key in ("交易账号", "mini路径")
                if not str(live_setting.get(key) or "").strip()
            ]
            if missing:
                raise ValueError(
                    f"真实 QMT gateway {gw['name']} 缺少连接配置字段: {missing}; "
                    "请传 --qmt-account 并配置 QMT_CLIENT_PATH"
                )
        gw_names.add(gw["name"])

    for strategy in strategies:
        if strategy["gateway_name"] not in gw_names:
            raise ValueError(
                f"策略 {strategy['strategy_name']} 引用了未注册 gateway "
                f"{strategy['gateway_name']}"
            )

    print(f"[validate] {live_count} live/fake_live, {len(gateways) - live_count} sim")


def _load_gateway_class(kind: str):
    """Load gateway class by logical kind."""
    if kind == "sim":
        from vnpy_qmt_sim import QmtSimGateway

        return QmtSimGateway
    if kind == "fake_live":
        from vnpy_ml_strategy.test.fakes.fake_qmt_gateway import FakeQmtGateway

        return FakeQmtGateway
    if kind == "live":
        from vnpy_qmt import QmtGateway

        return QmtGateway
    raise ValueError(f"unknown gateway kind={kind!r}")


def _make_strategy_class(
    class_name: str,
    strategy_name: str,
    gateway_name: str,
    setting_path: Path,
    final_settle_day: date | None = None,
    runtime: Optional[Dict[str, Any]] = None,
) -> Type[Any]:
    """Create a RedisLiveSimTestStrategy subclass bound to one name/gateway."""
    from vnpy_signal_strategy_plus.strategies import redis_live_sim_test_strategy as redis_mod

    redis_mod.REDIS_LIVE_SIM_SETTING_PATH = setting_path
    base_class = redis_mod.RedisLiveSimTestStrategy
    runtime = dict(runtime or {})

    def load_external_setting(self: Any) -> None:
        base_class.load_external_setting(self)
        if "replay_enabled" in runtime:
            self._replay_enabled = bool(runtime["replay_enabled"])
        self.live_orders_enabled = bool(runtime.get("live_orders_enabled", True))
        self.live_signal_cutoff_dt = runtime.get("live_signal_cutoff_dt")
        self.runner_runtime_role = str(runtime.get("role", "sim"))
        self.signal_source_stg = str(runtime.get("signal_source_stg") or strategy_name)
        self.signal_application_scope_suffix = str(
            runtime.get("application_scope_suffix") or ""
        )
        if final_settle_day is not None and bool(getattr(self, "_replay_enabled", False)):
            self._final_settle_day = final_settle_day
            self.write_log(f"[dual-track] replay settle_through={final_settle_day}")
        self.gateway = gateway_name
        self.write_log(
            f"[dual-track] strategy_name={strategy_name} gateway override={gateway_name} "
            f"signal_source_stg={self.signal_source_stg} "
            f"application_scope_suffix={self.signal_application_scope_suffix or '-'} "
            f"role={self.runner_runtime_role} replay_enabled={getattr(self, '_replay_enabled', None)} "
            f"live_orders_enabled={self.live_orders_enabled} "
            f"live_signal_cutoff={self.live_signal_cutoff_dt}"
        )

    attrs = {
        "strategy_name": strategy_name,
        "author": f"redis-dual-track:{gateway_name}",
        "load_external_setting": load_external_setting,
        "__module__": __name__,
    }
    return type(class_name, (base_class,), attrs)


def _cleanup_runner_state(
    setting: Dict[str, Any],
    cleanup_strategy_names: Iterable[str],
    gateway_names: Iterable[str],
    shadow_stg: Optional[str],
    application_scope_suffix: str = "",
) -> None:
    """Clean only runner-owned local state and shadow MySQL rows."""
    print("=" * 60)
    print("Step 1 · 清理 Redis 双轨 runner 状态")
    print("=" * 60)

    sim_cfg = setting.get("sim", {}) or {}
    state_dir = _resolve_sim_state_dir(setting)
    for gw_name in gateway_names:
        for suffix in (".db", ".db-shm", ".db-wal", ".lock"):
            p = state_dir / f"sim_{gw_name}{suffix}"
            if p.exists():
                try:
                    p.unlink()
                    print(f"  删 {p}")
                except OSError as exc:
                    print(f"  ⚠️ 删 {p} 失败: {exc}")

    journal_db = _strategy_equity_journal_db()
    if journal_db.exists():
        try:
            con = sqlite3.connect(str(journal_db), timeout=2)
            names = list(cleanup_strategy_names)
            if names:
                placeholders = ",".join("?" * len(names))
                deleted = con.execute(
                    f"DELETE FROM strategy_equity_journal "
                    f"WHERE strategy_name IN ({placeholders})",
                    names,
                ).rowcount
                con.commit()
                print(f"  删 strategy_equity_journal.db 策略快照: {deleted}")
            con.close()
        except Exception as exc:
            print(f"  ⚠️ 清 strategy_equity_journal.db 失败: {exc}")

    mlearnweb_db_path = os.getenv("MLEARNWEB_DB")
    if mlearnweb_db_path:
        mlearnweb_db = Path(mlearnweb_db_path)
        if mlearnweb_db.exists():
            try:
                con = sqlite3.connect(str(mlearnweb_db), timeout=2)
                names = list(cleanup_strategy_names)
                if names:
                    placeholders = ",".join("?" * len(names))
                    deleted = con.execute(
                        f"DELETE FROM strategy_equity_snapshots "
                        f"WHERE strategy_name IN ({placeholders})",
                        names,
                    ).rowcount
                    con.commit()
                    print(f"  删 mlearnweb strategy_equity_snapshots: {deleted}")
                con.close()
            except Exception as exc:
                print(f"  ⚠️ 清 mlearnweb.db 失败: {exc}")

    _delete_strategy_application_rows(
        setting,
        cleanup_strategy_names,
        application_scope_suffix=application_scope_suffix,
    )

    if shadow_stg:
        _delete_shadow_mysql_rows(setting, shadow_stg)

    print("[cleanup] done\n")


def _mysql_url(mysql_cfg: Dict[str, Any]) -> str:
    user = quote_plus(str(mysql_cfg.get("user", "")))
    password = quote_plus(str(mysql_cfg.get("password", "")))
    host = str(mysql_cfg.get("host", "127.0.0.1"))
    port = int(mysql_cfg.get("port", 3306))
    db = str(mysql_cfg.get("db", "mysql"))
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{db}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _mysql_connect_args(mysql_cfg: Dict[str, Any]) -> Dict[str, int]:
    """Return bounded PyMySQL socket timeouts for runner-owned DB work."""
    return {
        "connect_timeout": _coerce_positive_int(
            mysql_cfg.get("connect_timeout"),
            DEFAULT_MYSQL_CONNECT_TIMEOUT,
        ),
        "read_timeout": _coerce_positive_int(
            mysql_cfg.get("read_timeout"),
            DEFAULT_MYSQL_READ_TIMEOUT,
        ),
        "write_timeout": _coerce_positive_int(
            mysql_cfg.get("write_timeout"),
            DEFAULT_MYSQL_WRITE_TIMEOUT,
        ),
    }


def _run_with_timeout(func: Any, *, timeout: int, label: str) -> Any:
    """Run a blocking call in a daemon thread and fail if it exceeds timeout."""
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            result_queue.put((True, func()))
        except BaseException as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=_target, name=f"{label}-timeout", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{label} timed out after {timeout}s")

    ok, value = result_queue.get_nowait()
    if ok:
        return value
    raise value


def _mysql_dbapi_connect(mysql_cfg: Dict[str, Any]) -> Any:
    """Create a PyMySQL connection with a hard 10s startup deadline."""
    import pymysql

    args = _mysql_connect_args(mysql_cfg)

    def _connect() -> Any:
        return pymysql.connect(
            host=str(mysql_cfg.get("host", "127.0.0.1")),
            port=int(mysql_cfg.get("port", 3306)),
            user=str(mysql_cfg.get("user", "")),
            password=str(mysql_cfg.get("password", "")),
            database=str(mysql_cfg.get("db", "mysql")),
            charset=str(mysql_cfg.get("charset", "utf8mb4")),
            connect_timeout=args["connect_timeout"],
            read_timeout=args["read_timeout"],
            write_timeout=args["write_timeout"],
        )

    return _run_with_timeout(
        _connect,
        timeout=args["connect_timeout"],
        label="MySQL connection",
    )


def _mysql_engine(mysql_cfg: Dict[str, Any], *, pool_pre_ping: bool = False):
    """Create a SQLAlchemy engine with finite PyMySQL socket timeouts."""
    from sqlalchemy import create_engine

    return create_engine(
        _mysql_url(mysql_cfg),
        creator=lambda: _mysql_dbapi_connect(mysql_cfg),
        pool_pre_ping=pool_pre_ping,
    )


def _delete_shadow_mysql_rows(setting: Dict[str, Any], shadow_stg: str) -> None:
    """Delete v2 shadow signal rows and their consumption checkpoints."""
    try:
        from sqlalchemy import text

        engine = _mysql_engine(setting["mysql"])
        with engine.begin() as conn:
            app_deleted = conn.execute(
                text(
                    "DELETE FROM strategy_signal_applications "
                    "WHERE strategy_name=:stg OR signal_event_id IN ("
                    "  SELECT id FROM trade_signal_events WHERE stg=:stg"
                    ")"
                ),
                {"stg": shadow_stg},
            ).rowcount
            event_deleted = conn.execute(
                text("DELETE FROM trade_signal_events WHERE stg=:stg"),
                {"stg": shadow_stg},
            ).rowcount
        engine.dispose()
        print(
            f"  deleted MySQL shadow v2 rows stg={shadow_stg!r}: "
            f"events={event_deleted} applications={app_deleted}"
        )
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"  warn: purge MySQL shadow v2 rows failed: {exc}")


def _delete_strategy_application_rows(
    setting: Dict[str, Any],
    strategy_names: Iterable[str],
    *,
    application_scope_suffix: str = "",
) -> None:
    """Delete v2 consumption checkpoints while keeping source signal events."""
    names = [str(name) for name in strategy_names if str(name)]
    if not names:
        return
    try:
        from sqlalchemy import bindparam, text

        engine = _mysql_engine(setting["mysql"])
        sql = (
            "DELETE FROM strategy_signal_applications "
            "WHERE strategy_name IN :names"
        )
        params: Dict[str, Any] = {"names": names}
        scope_suffix = str(application_scope_suffix or "").strip()
        if scope_suffix:
            sql += " AND account_id LIKE :account_suffix"
            params["account_suffix"] = f"%@{scope_suffix}"
        stmt = text(sql).bindparams(bindparam("names", expanding=True))
        with engine.begin() as conn:
            deleted = conn.execute(stmt, params).rowcount
        engine.dispose()
        suffix_note = f" account_suffix=@{scope_suffix}" if scope_suffix else ""
        print(
            f"  deleted MySQL v2 application checkpoints "
            f"strategies={names}{suffix_note}: {deleted}"
        )
    except TimeoutError:
        raise
    except Exception as exc:
        print(f"  warn: purge MySQL v2 application checkpoints failed: {exc}")


@dataclass
class MirrorStats:
    copied: int = 0
    last_source_id: int = 0


class MySqlSignalMirror:
    """Mirror source v2 signal events into an independent shadow ``stg``."""

    def __init__(
        self,
        mysql_cfg: Dict[str, Any],
        source_stg: str,
        target_stg: str,
        *,
        poll_interval: float = 0.5,
        mirror_existing_unprocessed: bool = True,
    ) -> None:
        self.mysql_cfg = mysql_cfg
        self.source_stg = source_stg
        self.target_stg = target_stg
        self.poll_interval = float(poll_interval)
        # Kept for CLI compatibility. In v2 the source event is append-only and
        # checkpoint is per strategy, so existing source events can be mirrored
        # safely; upsert by deterministic mirror uid keeps the operation idempotent.
        self.mirror_existing_unprocessed = mirror_existing_unprocessed
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.stats = MirrorStats()
        self._engine = None
        self._Session = None

    def start(self) -> None:
        """Start the background mirror thread."""
        from sqlalchemy.orm import sessionmaker
        from vnpy_signal_strategy_plus.signal_journal import SignalJournalBase

        self._engine = _mysql_engine(self.mysql_cfg, pool_pre_ping=True)
        SignalJournalBase.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine)
        self._bootstrap_last_id()
        self.thread = threading.Thread(target=self._run, name="mysql-signal-mirror", daemon=True)
        self.thread.start()
        print(
            f"[mirror] MySQL trade_signal_events {self.source_stg!r} -> {self.target_stg!r} "
            f"started, last_source_id={self.stats.last_source_id}"
        )

    def stop(self) -> None:
        """Stop the mirror thread and dispose the SQLAlchemy engine."""
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        if self._engine is not None:
            self._engine.dispose()
        print(f"[mirror] stopped, copied={self.stats.copied}")

    def _bootstrap_last_id(self) -> None:
        assert self._Session is not None
        session = self._Session()
        try:
            if self.mirror_existing_unprocessed:
                rows = self._query_source_rows(session)
                self._insert_rows(session, rows)
            self.stats.last_source_id = self._max_source_id(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._mirror_once()
            except Exception as exc:
                print(f"[mirror] copy failed: {exc}")
                time.sleep(2)
            self.stop_event.wait(self.poll_interval)

    def _mirror_once(self) -> None:
        assert self._Session is not None
        session = self._Session()
        try:
            rows = self._query_source_rows(session, min_id=self.stats.last_source_id)
            self._insert_rows(session, rows)
            if rows:
                self.stats.last_source_id = int(rows[-1].id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _query_source_rows(self, session: Any, min_id: int | None = None) -> list[Any]:
        from vnpy_signal_strategy_plus.signal_journal import TradeSignalEvent

        query = session.query(TradeSignalEvent).filter(TradeSignalEvent.stg == self.source_stg)
        if min_id is not None:
            query = query.filter(TradeSignalEvent.id > int(min_id))
        return query.order_by(TradeSignalEvent.id.asc()).all()

    def _max_source_id(self, session: Any) -> int:
        from sqlalchemy import func
        from vnpy_signal_strategy_plus.signal_journal import TradeSignalEvent

        value = (
            session.query(func.coalesce(func.max(TradeSignalEvent.id), 0))
            .filter(TradeSignalEvent.stg == self.source_stg)
            .scalar()
        )
        return int(value or 0)

    def _insert_rows(self, session: Any, rows: Iterable[Any]) -> None:
        from vnpy_signal_strategy_plus.signal_journal import (
            PCT_SEMANTICS,
            normalize_trade_signal_payload,
            upsert_trade_signal_event,
        )

        rows = list(rows)
        if not rows:
            return

        copied = 0
        for row in rows:
            digest = hashlib.sha1(str(row.signal_uid).encode("utf-8")).hexdigest()[:32]
            signal_uid = f"mirror:{self.target_stg}:{digest}"
            try:
                payload = json.loads(row.raw_payload or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            payload.update(
                {
                    "source": "mirror",
                    "source_signal_id": f"mirror:{self.target_stg}:{digest}",
                    "signal_uid": signal_uid,
                    "code": row.code,
                    "pct": row.pct,
                    "pct_semantics": PCT_SEMANTICS,
                    "amt": row.amt,
                    "type": row.signal_type,
                    "price": row.price,
                    "stg": self.target_stg,
                    "remark": row.remark.strftime("%Y-%m-%d %H:%M:%S"),
                    "empty": int(bool(row.empty)),
                    "source_event_id": row.id,
                    "source_stg": self.source_stg,
                }
            )
            normalized = normalize_trade_signal_payload(
                payload,
                target_stg=self.target_stg,
                stream_key=row.stream_key,
                redis_id=row.redis_id,
                source="mirror",
            )
            _target_row, created = upsert_trade_signal_event(session, normalized)
            if created:
                copied += 1

        if copied:
            self.stats.copied += copied
            print(f"[mirror] copied {copied} v2 events -> {self.target_stg}")

def _drain_proc_output(proc: subprocess.Popen[str], prefix: str) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        print(f"[{prefix}] {line.rstrip()}")


def _start_webtrader(main_engine: Any, setting: Dict[str, Any]) -> Optional[subprocess.Popen[str]]:
    """Start WebTrader RPC server and optional uvicorn HTTP child."""
    web_cfg = setting.get("webtrader", {}) or {}
    if not web_cfg.get("enable", True):
        print("[webtrader] disabled by config")
        return None

    web_engine = main_engine.get_engine("RpcService")
    if web_engine is None:
        print("[webtrader] RpcService engine not found, skip")
        return None

    rep = str(web_cfg.get("rep_address", "tcp://127.0.0.1:2014"))
    pub = str(web_cfg.get("pub_address", "tcp://127.0.0.1:4102"))
    set_node = getattr(web_engine, "set_node_info", None)
    if callable(set_node):
        try:
            set_node(
                node_id=web_cfg.get("node_id", "redis-dual-track"),
                display_name=web_cfg.get("display_name", "redis-dual-track"),
            )
        except Exception as exc:
            print(f"[webtrader] set_node_info failed: {exc}")

    try:
        web_engine.start_server(rep, pub)
        print(f"[webtrader] RPC started REP={rep} PUB={pub}")
    except Exception as exc:
        print(f"[webtrader] RPC start failed, skip uvicorn: {exc}")
        return None

    host = str(web_cfg.get("http_host", "127.0.0.1"))
    port = str(web_cfg.get("http_port", WEBTRADER_HTTP_PORT))
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "vnpy_webtrader.web:app",
        f"--host={host}",
        f"--port={port}",
    ]
    child_env = dict(os.environ)
    child_env["VNPY_WEB_REQ_ADDRESS"] = rep
    child_env["VNPY_WEB_SUB_ADDRESS"] = pub
    child_env["VNPY_DATA_ROOT"] = str(vnpy_data_root())
    sim_state_dir = _resolve_sim_state_dir(setting)
    if sim_state_dir.resolve() != state_dir().resolve():
        child_env["VNPY_QMT_SIM_TRADING_STATE"] = str(sim_state_dir)

    proc = subprocess.Popen(
        cmd,
        cwd=str(_HERE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_env,
    )
    threading.Thread(target=_drain_proc_output, args=(proc, "uvicorn"), daemon=True).start()
    print(f"[webtrader] uvicorn pid={proc.pid} -> http://{host}:{port}/docs")
    return proc


def _start_vnpy(
    setting_path: Path,
    setting: Dict[str, Any],
    gateways: List[Dict[str, Any]],
    strategies: List[Dict[str, Any]],
    *,
    start_webtrader: bool = True,
    final_settle_day: date | None = None,
    daily_data_ingest: Optional[Dict[str, Any]] = None,
) -> tuple[Any, Optional[subprocess.Popen[str]], List[str]]:
    """Start MainEngine, gateways, SignalStrategyPlus and strategy instances."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_signal_strategy_plus import SignalStrategyPlusApp
    from vnpy_webtrader import WebTraderApp

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    for gw in gateways:
        cls = _load_gateway_class(gw["kind"])
        print(f"[boot] add_gateway kind={gw['kind']} name={gw['name']} class={cls.__name__}")
        main_engine.add_gateway(cls, gateway_name=gw["name"])

    if daily_data_ingest and daily_data_ingest.get("enable"):
        _configure_daily_data_ingest(daily_data_ingest)
        from vnpy_tushare_pro import TushareProApp
        from vnpy_tushare_pro.engine import APP_NAME as TUSHARE_APP_NAME

        main_engine.add_app(TushareProApp)
        tushare_engine = main_engine.get_engine(TUSHARE_APP_NAME)
        if tushare_engine is None:
            raise RuntimeError("TusharePro engine not found after enabling daily data ingest")
        tushare_engine.init_engine()
        print(
            "[boot] daily data ingest enabled "
            f"mode={daily_data_ingest['mode']} time={daily_data_ingest['time']}"
        )

    main_engine.add_app(SignalStrategyPlusApp)
    if start_webtrader:
        main_engine.add_app(WebTraderApp)

    for gw in gateways:
        print(f"[boot] connecting gateway {gw['name']}...")
        main_engine.connect(gw["setting"], gw["name"])

    signal_engine = main_engine.get_engine("SignalStrategyPlus")
    signal_engine.init_engine()
    print(f"[boot] SignalStrategyPlus classes loaded: {signal_engine.get_all_strategy_class_names()}")

    started: List[str] = []
    for strategy_def in strategies:
        cls = _make_strategy_class(
            strategy_def["class_name"],
            strategy_def["strategy_name"],
            strategy_def["gateway_name"],
            setting_path,
            final_settle_day,
            strategy_def.get("runtime"),
        )
        signal_engine.add_strategy(cls)
        name = strategy_def["strategy_name"]
        if name not in signal_engine.strategies:
            print(f"[boot] add_strategy({name}) failed")
            continue
        if not signal_engine.init_strategy(name):
            print(f"[boot] init_strategy({name}) failed")
            continue
        signal_engine.start_strategy(name)
        started.append(name)
        print(f"[boot] strategy started {name} -> gateway={strategy_def['gateway_name']}")

    web_proc = _start_webtrader(main_engine, setting) if start_webtrader else None
    return main_engine, web_proc, started


def _print_verification(
    setting: Dict[str, Any],
    strategy_names: List[str],
    sim_gateway_names: List[str],
    shadow_stg: Optional[str],
) -> None:
    """Print post-run verification commands."""
    print("\n" + "=" * 60)
    print("退出后验证 cmd")
    print("=" * 60)

    sim_cfg = setting.get("sim", {}) or {}
    state_dir = _resolve_sim_state_dir(setting)
    print("\n# (a) sim DB 隔离: 每个模拟 gateway 一个 sqlite")
    for gw_name in sim_gateway_names:
        print(f"  sqlite3 {state_dir}/sim_{gw_name}.db \"SELECT COUNT(*) FROM sim_trades\"")

    print("\n# (b) 成交流水按策略 reference 前缀归属")
    for gw_name in sim_gateway_names:
        for stg in strategy_names:
            print(
                f"  sqlite3 {state_dir}/sim_{gw_name}.db "
                f"\"SELECT COUNT(*) FROM sim_trades WHERE reference LIKE '{stg}:%'\""
            )

    if shadow_stg:
        print("\n# (c) MySQL v2 shadow signal mirror check")
        print(
            "  SELECT stg, COUNT(*), MIN(remark), MAX(remark) "
            f"FROM trade_signal_events WHERE stg IN ('{strategy_names[0]}','{shadow_stg}') "
            "GROUP BY stg;"
        )
        print(
            "  SELECT strategy_name, gateway_name, COUNT(*) "
            f"FROM strategy_signal_applications WHERE strategy_name IN ('{strategy_names[0]}','{shadow_stg}') "
            "GROUP BY strategy_name, gateway_name;"
        )

    print("\n# (d) strategy_equity_journal")
    print(
        f"  sqlite3 {_strategy_equity_journal_db()} "
        "\"SELECT engine, strategy_name, source_label, COUNT(*), MIN(ts), MAX(ts) "
        "FROM strategy_equity_journal GROUP BY engine, strategy_name, source_label\""
    )

    print("\n# (e) WebTrader")
    print("  http://127.0.0.1:8001/docs")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["single", "v1", "v2", "v3"],
        required=True,
        help="v1/single=单 QMT_SIM；v2=FakeQmt+sim shadow；v3=真 QMT 实时 source + sim shadow",
    )
    parser.add_argument(
        "--config",
        default=str(_default_setting_path()),
        help=(
            "Signal dual-track runtime config. Default: SIGNAL_DUAL_TRACK_CONFIG "
            "or <VNPY_DATA_ROOT>/config/signal_dual_track.json"
        ),
    )
    parser.add_argument(
        "--source-stg",
        default="",
        help="source v2 trade_signal_events.stg; default config.strategy_name",
    )
    parser.add_argument(
        "--shadow-stg",
        default="",
        help=(
            "shadow v2 trade_signal_events.stg; default "
            "<source-stg>_shadow_<runner-id> in v2/v3"
        ),
    )
    parser.add_argument(
        "--runner-id",
        default="",
        help=(
            "unique runner/deployment id for mirror shadow stg isolation; "
            f"can also be set by {SIGNAL_RUNNER_ID_ENV} or config runner_id"
        ),
    )
    parser.add_argument(
        "--allow-shared-shadow-stg",
        action="store_true",
        help=(
            "allow the legacy shared <source-stg>_shadow name; only use for "
            "isolated one-off tests"
        ),
    )
    parser.add_argument(
        "--qmt-account",
        default="",
        help="required for v3: broker paper/live account id",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="兼容旧参数：等价于 --cleanup-scope none",
    )
    parser.add_argument(
        "--cleanup-scope",
        choices=["default", "none", "shadow", "all-sim"],
        default="default",
        help=(
            "启动前清理范围。default: v3=shadow, v1/v2=all-sim；"
            "shadow 不会删除 v3 真实 source checkpoint"
        ),
    )
    parser.add_argument(
        "--no-webtrader",
        action="store_true",
        help="do not start WebTrader RPC/HTTP service",
    )
    parser.add_argument(
        "--enable-daily-data-ingest",
        action="store_true",
        help="enable the Tushare scheduled daily download task in this runner",
    )
    parser.add_argument(
        "--daily-data-time",
        default="",
        help="daily data ingest wall-clock time, default 20:00",
    )
    parser.add_argument(
        "--daily-data-mode",
        choices=["fetch-only", "skip-dump", "full"],
        default="",
        help=(
            "fetch-only updates merged/QMT_SIM snapshots only; skip-dump also runs "
            "filter/by_stock; full keeps the qlib dump"
        ),
    )
    parser.add_argument(
        "--no-mirror-existing-unprocessed",
        action="store_true",
        help="do not mirror existing source v2 events; only mirror newly inserted events",
    )
    parser.add_argument(
        "--shadow-replay",
        choices=["historical", "none"],
        default="historical",
        help="shadow QMT_SIM 腿是否回放历史镜像信号",
    )
    parser.add_argument(
        "--allow-live-orders",
        action="store_true",
        help=(
            "仅 v3 有效：显式武装真实 QMT source 腿；不传则 source "
            "不轮询/消费 MySQL 信号，也不会向券商发单"
        ),
    )
    parser.add_argument(
        "--live-signal-cutoff",
        choices=["startup", "today"],
        default="startup",
        help=(
            "仅 v3 且 --allow-live-orders 有效：真实 source 信号消费下界。"
            "startup 只消费启动后新信号，today 允许消费今日未消费信号"
        ),
    )
    parser.add_argument(
        "--settle-through",
        default="",
        help="settle no-signal tail through this date, e.g. 2026-05-11",
    )
    args = parser.parse_args()

    mode = _normalize_mode(args.mode)
    setting_path = Path(args.config).resolve()
    if not setting_path.exists():
        raise FileNotFoundError(
            f"signal dual-track config not found: {setting_path}. "
            f"Set {SIGNAL_DUAL_TRACK_CONFIG_ENV} or copy "
            f"config/signal_dual_track.example.json to "
            f"{config_dir() / DEFAULT_SETTING_FILENAME}."
        )
    setting = _load_json(setting_path)
    daily_data_ingest = _resolve_daily_data_ingest_config(args, setting)
    final_settle_day = _parse_day(args.settle_through)
    if args.settle_through:
        print(f"[config] replay.settle_through={final_settle_day} (cli)")
    else:
        final_settle_day = _latest_completed_trade_day(setting)
        if final_settle_day is not None:
            print(f"[config] replay.settle_through={final_settle_day} (latest completed trade day)")
    journal_db = _strategy_equity_journal_db()
    source_stg = args.source_stg or str(setting.get("strategy_name") or "").strip()
    if not source_stg:
        raise ValueError("strategy_name is required in config unless --source-stg is provided")
    source_stg = _validate_stg_name(source_stg, field="source_stg")
    runner_id = _resolve_runner_id(args.runner_id, setting)
    shadow_stg = _resolve_shadow_stg(
        mode,
        source_stg,
        args.shadow_stg,
        runner_id,
        allow_shared_shadow_stg=args.allow_shared_shadow_stg,
    )

    live_signal_cutoff_dt = _resolve_live_signal_cutoff(mode, args.live_signal_cutoff)
    cfg = _build_config(
        mode,
        setting,
        source_stg,
        shadow_stg,
        args.qmt_account,
        runner_id=runner_id,
        shadow_replay=args.shadow_replay,
    )
    gateways = cfg["GATEWAYS"]
    strategies = cfg["STRATEGIES"]
    _apply_live_runtime_options(
        strategies,
        mode=mode,
        allow_live_orders=args.allow_live_orders,
        live_signal_cutoff_dt=live_signal_cutoff_dt,
    )
    gateway_names = [g["name"] for g in gateways]
    sim_gateway_names = [
        g["name"] for g in gateways if g["kind"] in {"sim", "fake_live"}
    ]
    strategy_names = [s["strategy_name"] for s in strategies]

    print("=" * 60)
    print(f"RedisLiveSim 双轨 runner — {mode.upper()}")
    print("=" * 60)
    print(f"config:     {setting_path}")
    print(f"模式:       {cfg['label']}")
    print(f"runner_id:  {runner_id or '-'}")
    print(f"GATEWAYS:   {gateway_names}")
    print(f"STRATEGIES: {strategy_names}")
    print(f"strategy_equity_journal.db: {journal_db}")
    if daily_data_ingest["enable"]:
        print(
            "daily_data_ingest: "
            f"enabled mode={daily_data_ingest['mode']} time={daily_data_ingest['time']}"
        )
    if cfg["mirror"]:
        print(f"MySQL mirror: {source_stg!r} -> {shadow_stg!r}")
    cleanup_scope = "none" if args.no_cleanup else args.cleanup_scope
    if cleanup_scope == "default":
        cleanup_scope = _default_cleanup_scope(mode)
    print(f"cleanup_scope: {cleanup_scope}")
    if mode == "v3":
        print(
            "[safety] v3 source leg uses real QMT, replay is disabled. "
            f"allow_live_orders={args.allow_live_orders} "
            f"live_signal_cutoff={live_signal_cutoff_dt}"
        )
        if not args.allow_live_orders:
            print("[safety] real source signal polling is disabled; no broker orders can be sent")
    elif args.allow_live_orders:
        print("[safety] --allow-live-orders is ignored outside v3")
    print()

    _validate_config(gateways, strategies)

    cleanup_strategy_names = _strategy_names_for_cleanup(
        strategies,
        sim_gateway_names,
        cleanup_scope,
    )
    if cleanup_scope != "none" and cleanup_strategy_names:
        _cleanup_runner_state(
            setting,
            cleanup_strategy_names=cleanup_strategy_names,
            gateway_names=sim_gateway_names,
            shadow_stg=shadow_stg if cfg["mirror"] else None,
            application_scope_suffix=runner_id,
        )
    else:
        print(f"[cleanup] skipped scope={cleanup_scope} strategies={cleanup_strategy_names}")

    mirror: Optional[MySqlSignalMirror] = None
    if cfg["mirror"]:
        mirror = MySqlSignalMirror(
            setting["mysql"],
            source_stg,
            shadow_stg,
            poll_interval=float((setting.get("strategy", {}) or {}).get("poll_interval", 0.5)),
            mirror_existing_unprocessed=not args.no_mirror_existing_unprocessed,
        )
        mirror.start()

    main_engine = None
    web_proc: Optional[subprocess.Popen[str]] = None

    def _signal_handler(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        main_engine, web_proc, started = _start_vnpy(
            setting_path,
            setting,
            gateways,
            strategies,
            start_webtrader=not args.no_webtrader,
            final_settle_day=final_settle_day,
            daily_data_ingest=daily_data_ingest,
        )
        if not started:
            raise RuntimeError("没有策略成功启动")
        print(f"\n[ready] {len(started)} strategies started: {started}. Ctrl+C 退出.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[shutdown] 收到退出信号")
    finally:
        if main_engine is not None:
            try:
                signal_engine = main_engine.get_engine("SignalStrategyPlus")
                for name in strategy_names:
                    if name in signal_engine.strategies:
                        print(f"[shutdown] stop_strategy({name})")
                        signal_engine.stop_strategy(name)
            except Exception as exc:
                print(f"[shutdown] stop strategies failed: {exc}")

        if mirror is not None:
            mirror.stop()

        if web_proc is not None and web_proc.poll() is None:
            print("[shutdown] terminating uvicorn")
            web_proc.terminate()
            try:
                web_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                web_proc.kill()

        if main_engine is not None:
            print("[shutdown] main_engine.close()")
            main_engine.close()

        _print_verification(
            setting,
            strategy_names=strategy_names,
            sim_gateway_names=sim_gateway_names,
            shadow_stg=shadow_stg if cfg["mirror"] else None,
        )

if __name__ == "__main__":
    main()
