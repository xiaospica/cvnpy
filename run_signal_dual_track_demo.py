# -*- coding: utf-8 -*-
"""Redis signal strategy live/sim dual-track demo.

This script is the SignalStrategyPlus counterpart of ``run_dual_track_demo.py``.
It targets ``RedisLiveSimTestStrategy`` and supports:

  --mode single
      One QMT_SIM gateway + one RedisLiveSimTestStrategy instance.

  --mode v2
      FakeQmtGateway named QMT + QMT_SIM shadow.  Both use sim matching, but
      validate the live gateway slot, gateway routing, account DB isolation and
      WebTrader/mlearnweb display without miniQMT risk.

  --mode v3
      Real QmtGateway named QMT + QMT_SIM shadow.  Requires a broker paper
      account and a running miniQMT client.

Unlike MLStrategy, RedisLiveSimTestStrategy reads MySQL ``stock_trade`` rows and
marks them ``processed=True``.  For dual-track modes this script mirrors source
rows into a shadow ``stg`` so live and shadow can consume independent rows while
keeping identical signal payloads.

Examples:
    F:/Program_Home/vnpy/python.exe run_signal_dual_track_demo.py --mode single
    F:/Program_Home/vnpy/python.exe run_signal_dual_track_demo.py --mode v2
    F:/Program_Home/vnpy/python.exe run_signal_dual_track_demo.py --mode v3 --qmt-account YOUR_PAPER_ACCOUNT
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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

if load_dotenv is not None:
    _DOTENV_FILE = os.getenv("DOTENV_FILE")
    if _DOTENV_FILE and (_HERE / _DOTENV_FILE).exists():
        load_dotenv(_HERE / _DOTENV_FILE, override=False)
    elif (_HERE / ".env.production").exists():
        load_dotenv(_HERE / ".env.production", override=False)
    elif (_HERE / ".env").exists():
        load_dotenv(_HERE / ".env", override=False)

from vnpy_common.data_paths import (  # noqa: E402
    ensure_vnpy_data_env,
    merged_snapshots_dir,
    state_dir,
    strategy_equity_journal_db_path,
    vnpy_data_root,
)

ensure_vnpy_data_env()


DEFAULT_SETTING_PATH = (
    _HERE / "vnpy_signal_strategy_plus" / "test" / "redis_live_sim_setting.json"
)
WEBTRADER_HTTP_PORT = 8001


def _resolve_setting_path(template_path: Path) -> Path:
    """Prefer a sibling ``.local.json`` file, then fall back to the template."""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


def _load_json(path: Path) -> Dict[str, Any]:
    """Load a UTF-8/UTF-8-SIG JSON file."""
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


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
    connect_setting.setdefault("merged_parquet_merged_root", str(merged_snapshots_dir()))
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
        "资金账号": qmt_account,
        "客户端路径": os.getenv(
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
) -> Dict[str, Any]:
    """Return gateway/strategy config for the requested mode."""
    if mode == "single":
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
                },
                {
                    "class_name": "RedisLiveSimV2Shadow",
                    "strategy_name": shadow_stg,
                    "gateway_name": shadow_gateway,
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
                },
                {
                    "class_name": "RedisLiveSimV3Shadow",
                    "strategy_name": shadow_stg,
                    "gateway_name": shadow_gateway,
                },
            ],
        }

    raise ValueError(f"unknown mode {mode!r}")


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
) -> Type[Any]:
    """Create a RedisLiveSimTestStrategy subclass bound to one name/gateway."""
    from vnpy_signal_strategy_plus.strategies import redis_live_sim_test_strategy as redis_mod

    redis_mod.REDIS_LIVE_SIM_SETTING_PATH = setting_path
    base_class = redis_mod.RedisLiveSimTestStrategy

    def load_external_setting(self: Any) -> None:
        base_class.load_external_setting(self)
        self.gateway = gateway_name
        self.write_log(
            f"[dual-track] strategy_name={strategy_name} gateway override={gateway_name}"
        )

    attrs = {
        "strategy_name": strategy_name,
        "author": f"redis-dual-track:{gateway_name}",
        "load_external_setting": load_external_setting,
        "__module__": __name__,
    }
    return type(class_name, (base_class,), attrs)


def _cleanup_demo_state(
    setting: Dict[str, Any],
    strategy_names: Iterable[str],
    gateway_names: Iterable[str],
    shadow_stg: Optional[str],
) -> None:
    """Clean only demo-owned local state and shadow MySQL rows."""
    print("=" * 60)
    print("Step 1 · 清理 Redis 双轨 demo 状态")
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
            names = list(strategy_names)
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
                names = list(strategy_names)
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


def _delete_shadow_mysql_rows(setting: Dict[str, Any], shadow_stg: str) -> None:
    """Delete shadow rows only; source Redis/MySQL rows are never removed here."""
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(_mysql_url(setting["mysql"]))
        with engine.begin() as conn:
            deleted = conn.execute(
                text("DELETE FROM stock_trade WHERE stg=:stg"),
                {"stg": shadow_stg},
            ).rowcount
        engine.dispose()
        print(f"  删 MySQL shadow rows stock_trade.stg={shadow_stg!r}: {deleted}")
    except Exception as exc:
        print(f"  ⚠️ 清 MySQL shadow rows 失败: {exc}")


@dataclass
class MirrorStats:
    copied: int = 0
    last_source_id: int = 0


class MySqlSignalMirror:
    """Mirror source ``stock_trade`` rows into an independent shadow ``stg``."""

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
        self.mirror_existing_unprocessed = mirror_existing_unprocessed
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.stats = MirrorStats()
        self._engine = None

    def start(self) -> None:
        """Start the background mirror thread."""
        from sqlalchemy import create_engine

        self._engine = create_engine(_mysql_url(self.mysql_cfg), pool_pre_ping=True)
        self._bootstrap_last_id()
        self.thread = threading.Thread(target=self._run, name="mysql-signal-mirror", daemon=True)
        self.thread.start()
        print(
            f"[mirror] MySQL stock_trade {self.source_stg!r} -> {self.target_stg!r} "
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
        from sqlalchemy import text

        assert self._engine is not None
        with self._engine.begin() as conn:
            if self.mirror_existing_unprocessed:
                rows = conn.execute(
                    text(
                        "SELECT id, code, pct, `type` AS signal_type, price, "
                        "remark, empty, amt, raw_payload "
                        "FROM stock_trade "
                        "WHERE stg=:source AND processed=0 "
                        "ORDER BY id ASC"
                    ),
                    {"source": self.source_stg},
                ).mappings().all()
                self._insert_rows(conn, rows)

            max_id = conn.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM stock_trade WHERE stg=:source"),
                {"source": self.source_stg},
            ).scalar_one()
            self.stats.last_source_id = int(max_id or 0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._mirror_once()
            except Exception as exc:
                print(f"[mirror] copy failed: {exc}")
                time.sleep(2)
            self.stop_event.wait(self.poll_interval)

    def _mirror_once(self) -> None:
        from sqlalchemy import text

        assert self._engine is not None
        with self._engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, code, pct, `type` AS signal_type, price, "
                    "remark, empty, amt, raw_payload "
                    "FROM stock_trade "
                    "WHERE stg=:source AND id>:last_id "
                    "ORDER BY id ASC"
                ),
                {"source": self.source_stg, "last_id": self.stats.last_source_id},
            ).mappings().all()
            self._insert_rows(conn, rows)
            if rows:
                self.stats.last_source_id = int(rows[-1]["id"])

    def _insert_rows(self, conn: Any, rows: Iterable[Any]) -> None:
        from sqlalchemy import text

        rows = list(rows)
        if not rows:
            return

        params = [
            {
                "code": row["code"],
                "pct": row["pct"],
                "type": row["signal_type"],
                "price": row["price"],
                "stg": self.target_stg,
                "remark": row["remark"],
                "empty": row["empty"],
                "amt": row["amt"],
                "raw_payload": row["raw_payload"],
            }
            for row in rows
        ]
        conn.execute(
            text(
                "INSERT INTO stock_trade "
                "(code, pct, `type`, price, stg, remark, processed, empty, amt, raw_payload) "
                "VALUES "
                "(:code, :pct, :type, :price, :stg, :remark, 0, :empty, :amt, :raw_payload)"
            ),
            params,
        )
        self.stats.copied += len(params)
        print(f"[mirror] copied {len(params)} rows -> {self.target_stg}")


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
        print("\n# (c) MySQL shadow 信号复制检查")
        print(
            "  SELECT stg, COUNT(*), SUM(processed=1) "
            f"FROM stock_trade WHERE stg IN ('{strategy_names[0]}','{shadow_stg}') "
            "GROUP BY stg;"
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
        choices=["single", "v2", "v3"],
        required=True,
        help="single=单 QMT_SIM / v2=FakeQmt+sim shadow / v3=真 QMT+sim shadow",
    )
    parser.add_argument(
        "--config",
        default=str(_resolve_setting_path(DEFAULT_SETTING_PATH)),
        help="RedisLiveSimTestStrategy 配置文件，默认优先 redis_live_sim_setting.local.json",
    )
    parser.add_argument(
        "--source-stg",
        default="",
        help="源 MySQL stock_trade.stg，默认取 config.strategy_name",
    )
    parser.add_argument(
        "--shadow-stg",
        default="",
        help="shadow MySQL stock_trade.stg，默认 <source-stg>_shadow",
    )
    parser.add_argument(
        "--qmt-account",
        default="",
        help="v3 必填: 券商仿真/实盘资金账号",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="跳过清理 sim DB、strategy_equity_journal、shadow MySQL rows",
    )
    parser.add_argument(
        "--no-webtrader",
        action="store_true",
        help="不启动 WebTrader RPC/HTTP 服务",
    )
    parser.add_argument(
        "--no-mirror-existing-unprocessed",
        action="store_true",
        help="v2/v3 启动时不复制源 stg 既有 processed=0 信号，只复制启动后的新增信号",
    )
    args = parser.parse_args()

    setting_path = Path(args.config).resolve()
    if not setting_path.exists():
        raise FileNotFoundError(f"config not found: {setting_path}")
    setting = _load_json(setting_path)
    journal_db = _strategy_equity_journal_db()
    source_stg = args.source_stg or str(setting.get("strategy_name") or "etf_rotation_basic")
    shadow_stg = args.shadow_stg or f"{source_stg}_shadow"

    cfg = _build_config(args.mode, setting, source_stg, shadow_stg, args.qmt_account)
    gateways = cfg["GATEWAYS"]
    strategies = cfg["STRATEGIES"]
    gateway_names = [g["name"] for g in gateways]
    sim_gateway_names = [
        g["name"] for g in gateways if g["kind"] in {"sim", "fake_live"}
    ]
    strategy_names = [s["strategy_name"] for s in strategies]

    print("=" * 60)
    print(f"RedisLiveSim 双轨 demo — {args.mode.upper()}")
    print("=" * 60)
    print(f"config:     {setting_path}")
    print(f"模式:       {cfg['label']}")
    print(f"GATEWAYS:   {gateway_names}")
    print(f"STRATEGIES: {strategy_names}")
    print(f"strategy_equity_journal.db: {journal_db}")
    if cfg["mirror"]:
        print(f"MySQL mirror: {source_stg!r} -> {shadow_stg!r}")
    print()

    _validate_config(gateways, strategies)

    if not args.no_cleanup:
        _cleanup_demo_state(
            setting,
            strategy_names=strategy_names,
            gateway_names=sim_gateway_names,
            shadow_stg=shadow_stg if cfg["mirror"] else None,
        )

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
