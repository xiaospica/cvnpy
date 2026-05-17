# -*- coding: utf-8 -*-
"""Purge local debug state for SignalStrategyPlus Redis/MySQL/QMT_SIM e2e runs.

Default behavior purges both the configured source strategy and its
``<strategy>_shadow`` pair, plus the common dual-track simulator accounts
``QMT`` and ``QMT_SIM_redis_shadow``.  The old ``stock_trade`` table is not part
of this cleanup path; runtime signals are v2 ``trade_signal_events`` rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import redis
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnpy_common.data_paths import ensure_vnpy_data_env, state_dir, strategy_equity_journal_db_path  # noqa: E402

PERSISTENCE_DIR_KEY = "\u6301\u4e45\u5316\u76ee\u5f55"


def resolve_setting_path(template_path: Path) -> Path:
    """Prefer a sibling .local.json file, falling back to the template."""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


def load_setting(path: Path) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _expand_config_path(value: object) -> str:
    ensure_vnpy_data_env()
    return os.path.expandvars(str(value)).strip()


def _resolve_sim_state_dir(setting: dict) -> Path:
    sim = setting.get("sim", {}) or {}
    raw = sim.get("db_dir") or (sim.get("connect_setting", {}) or {}).get(PERSISTENCE_DIR_KEY)
    return Path(_expand_config_path(raw)) if raw else state_dir()


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        out.append(text_value)
    return out


def resolve_strategy_names(setting: dict, args: argparse.Namespace) -> list[str]:
    """Resolve source and shadow strategy names to purge."""
    base = list(args.strategy_name or [])
    if not base:
        base = [str(setting["strategy_name"])]
    names = list(base)
    if not args.no_shadow:
        if args.shadow_stg:
            names.append(str(args.shadow_stg))
        else:
            names.extend(f"{name}_shadow" for name in base)
    return _unique(names)


def resolve_sim_accounts(setting: dict, args: argparse.Namespace) -> list[str]:
    """Resolve QMT_SIM persistence account names used by local debug runs."""
    if args.gateway_name:
        return _unique(args.gateway_name)
    sim = setting.get("sim", {}) or {}
    configured = [
        str(sim.get("account_id") or ""),
        str(sim.get("gateway_name") or ""),
        "QMT",
        "QMT_SIM_redis_shadow",
    ]
    return _unique(configured)


def confirm_or_abort(strategy_names: list[str], sim_accounts: list[str], args: argparse.Namespace) -> None:
    """Require confirmation before deleting local/remote debug state."""
    if args.yes:
        return
    print(f"[confirm] strategy_names={strategy_names}")
    print(f"[confirm] sim_accounts={sim_accounts}")
    answer = input("Type y to purge these debug states: ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("[purge] aborted")


def purge_mysql(setting: dict, strategy_names: list[str]) -> None:
    """Delete v2 signal journal rows and checkpoints for selected strategies."""
    m = setting["mysql"]
    url = f"mysql+pymysql://{m['user']}:{m['password']}@{m['host']}:{m['port']}/{m['db']}"
    engine = create_engine(url, pool_pre_ping=True)
    total_events = 0
    total_apps = 0
    with engine.begin() as conn:
        for stg in strategy_names:
            app_res = conn.execute(
                text(
                    "DELETE FROM strategy_signal_applications "
                    "WHERE strategy_name=:stg OR signal_event_id IN ("
                    "  SELECT id FROM trade_signal_events WHERE stg=:stg"
                    ")"
                ),
                {"stg": stg},
            )
            event_res = conn.execute(
                text("DELETE FROM trade_signal_events WHERE stg=:stg"),
                {"stg": stg},
            )
            total_apps += int(app_res.rowcount or 0)
            total_events += int(event_res.rowcount or 0)
            print(
                f"[mysql] DELETE v2 stg='{stg}' -> "
                f"events={event_res.rowcount} applications={app_res.rowcount}"
            )
    engine.dispose()
    print(f"[mysql] total deleted events={total_events} applications={total_apps}")


def purge_redis(setting: dict) -> None:
    r = setting["redis"]
    stream = r["stream_key"]
    rds = redis.Redis(
        host=r["host"],
        port=int(r["port"]),
        password=r.get("password") or None,
        db=int(r.get("db", 0)),
    )
    try:
        rds.xtrim(stream, maxlen=0, approximate=False)
        print(f"[redis] XTRIM {stream} MAXLEN 0 OK")
    except redis.ResponseError as exc:
        print(f"[redis] XTRIM skipped: {exc}")


def purge_sim_db(setting: dict, sim_accounts: list[str]) -> None:
    """Delete QMT_SIM SQLite persistence files for configured debug accounts."""
    sim_state_dir = _resolve_sim_state_dir(setting)
    for acc in sim_accounts:
        for suffix in (".db", ".db-shm", ".db-wal", ".lock"):
            p = sim_state_dir / f"sim_{acc}{suffix}"
            if p.exists():
                try:
                    p.unlink()
                    print(f"[sim] deleted {p}")
                except OSError as exc:
                    print(f"[sim] cannot delete {p} (still in use?): {exc}")
            else:
                print(f"[sim] skip {p} (not exists)")


def purge_strategy_equity_journal(setting: dict, strategy_names: list[str]) -> None:
    """Clear vnpy strategy equity journal rows for selected strategies."""
    db_path = strategy_equity_journal_db_path()
    if not db_path.exists():
        print(f"[strategy-equity-journal] skip {db_path} (not exists)")
        return

    try:
        with sqlite3.connect(str(db_path), timeout=20.0) as conn:
            total = 0
            for stg in strategy_names:
                res = conn.execute(
                    "DELETE FROM strategy_equity_journal WHERE strategy_name=?",
                    (stg,),
                )
                total += int(res.rowcount or 0)
                print(f"[strategy-equity-journal] DELETE strategy_name='{stg}' -> {res.rowcount} rows")
            conn.commit()
        print(f"[strategy-equity-journal] total deleted {total} rows")
    except sqlite3.Error as exc:
        print(f"[strategy-equity-journal] delete failed {db_path}: {exc}")


def warn_port_holders(setting: dict) -> None:
    """Warn about known WebTrader ports without killing any process."""
    try:
        import psutil
    except ImportError:
        print("[port] skipped (psutil not installed)")
        return

    web = setting.get("webtrader", {}) or {}
    rep = web.get("rep_address", "tcp://127.0.0.1:12014")
    pub = web.get("pub_address", "tcp://127.0.0.1:14102")
    http = web.get("http_port", "18001")

    def _port_of(zmq_addr: str) -> int:
        return int(str(zmq_addr).rsplit(":", 1)[-1])

    target_ports = {_port_of(rep), _port_of(pub), int(http), 2014, 4102, 8001}
    found = []
    for c in psutil.net_connections(kind="inet"):
        if c.status != psutil.CONN_LISTEN:
            continue
        if c.laddr and c.laddr.port in target_ports:
            try:
                proc = psutil.Process(c.pid)
                found.append((c.laddr.port, c.pid, proc.name(), proc.exe()))
            except psutil.NoSuchProcess:
                continue
    if found:
        print("[port] listeners found; stop them manually before deleting locked sim DBs:")
        for port, pid, name, exe in found:
            print(f"  port={port} PID={pid} name={name} exe={exe}")
    else:
        print("[port] key ports are idle")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge e2e/debug strategy state from mysql / redis / sim db"
    )
    parser.add_argument(
        "--config",
        default=str(resolve_setting_path(Path(__file__).resolve().parent / "test_setting.json")),
    )
    parser.add_argument("--strategy-name", action="append", default=[])
    parser.add_argument("--shadow-stg", default="")
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument("--gateway-name", action="append", default=[])
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--skip-mysql", action="store_true")
    parser.add_argument("--skip-redis", action="store_true")
    parser.add_argument("--skip-sim-db", action="store_true")
    parser.add_argument("--skip-equity-journal", action="store_true")
    args = parser.parse_args()

    setting = load_setting(Path(args.config))
    strategy_names = resolve_strategy_names(setting, args)
    sim_accounts = resolve_sim_accounts(setting, args)
    print(f"[purge] strategy_names={strategy_names}")
    print(f"[purge] sim_accounts={sim_accounts}")

    warn_port_holders(setting)
    confirm_or_abort(strategy_names, sim_accounts, args)

    if not args.skip_mysql:
        purge_mysql(setting, strategy_names)
    if not args.skip_redis:
        purge_redis(setting)
    if not args.skip_sim_db:
        purge_sim_db(setting, sim_accounts)
    if not args.skip_equity_journal:
        purge_strategy_equity_journal(setting, strategy_names)

    print("[purge] done")


if __name__ == "__main__":
    main()
