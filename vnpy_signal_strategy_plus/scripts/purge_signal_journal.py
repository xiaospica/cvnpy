# -*- coding: utf-8 -*-
"""Purge SignalStrategyPlus v2 MySQL signal journal rows by strategy.

This script intentionally touches only the canonical v2 tables:
``trade_signal_events`` and ``strategy_signal_applications``.  It never deletes
the legacy ``stock_trade`` table.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CONFIG = Path(__file__).resolve().with_name("redis_bridge_setting.json")


def resolve_setting_path(template_path: Path) -> Path:
    """Prefer a sibling ``.local.json`` config file."""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


def load_json(path: Path) -> dict[str, Any]:
    """Load a UTF-8 JSON config."""
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def unique(values: Iterable[str]) -> list[str]:
    """Return non-empty strings in stable unique order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        out.append(text_value)
    return out


def default_stgs(setting: dict[str, Any]) -> list[str]:
    """Resolve strategy names from bridge/test config shapes."""
    names: list[str] = []
    for sub in setting.get("subscriptions") or []:
        if isinstance(sub, dict):
            names.append(str(sub.get("target_stg") or sub.get("stream_key") or ""))
    if setting.get("strategy_name"):
        names.append(str(setting["strategy_name"]))
    return unique(names)


def resolve_stgs(setting: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Resolve stg names to purge."""
    names = list(args.stg or []) or default_stgs(setting)
    if not names:
        raise SystemExit("no stg resolved; pass --stg or use a config with subscriptions/strategy_name")
    resolved = list(names)
    if not args.no_shadow:
        if args.shadow_stg:
            resolved.extend(args.shadow_stg)
        else:
            resolved.extend(f"{name}_shadow" for name in names)
    return unique(resolved)


def mysql_engine(setting: dict[str, Any]):
    """Create a SQLAlchemy engine from the config mysql block."""
    cfg = setting["mysql"]
    url = URL.create(
        "mysql+pymysql",
        username=str(cfg["user"]),
        password=str(cfg["password"]),
        host=str(cfg["host"]),
        port=int(cfg.get("port") or 3306),
        database=str(cfg["db"]),
    )
    return create_engine(url, pool_pre_ping=True)


def fetch_summary(conn, stgs: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return signal and application summaries for the selected strategies."""
    event_stmt = (
        text(
            """
            SELECT stg, COUNT(*) AS cnt, MIN(id) AS min_id, MAX(id) AS max_id,
                   MIN(remark) AS min_remark, MAX(remark) AS max_remark
            FROM trade_signal_events
            WHERE stg IN :stgs
            GROUP BY stg
            ORDER BY stg
            """
        )
        .bindparams(bindparam("stgs", expanding=True))
    )
    app_stmt = (
        text(
            """
            SELECT strategy_name, gateway_name, status, COUNT(*) AS cnt,
                   MIN(signal_event_id) AS min_signal_event_id,
                   MAX(signal_event_id) AS max_signal_event_id
            FROM strategy_signal_applications
            WHERE strategy_name IN :stgs
               OR signal_event_id IN (
                   SELECT id FROM trade_signal_events WHERE stg IN :stgs
               )
            GROUP BY strategy_name, gateway_name, status
            ORDER BY strategy_name, gateway_name, status
            """
        )
        .bindparams(bindparam("stgs", expanding=True))
    )
    return (
        [dict(row) for row in conn.execute(event_stmt, {"stgs": stgs}).mappings()],
        [dict(row) for row in conn.execute(app_stmt, {"stgs": stgs}).mappings()],
    )


def print_summary(title: str, rows: list[dict[str, Any]]) -> None:
    """Print a compact table-like summary."""
    print(title)
    if not rows:
        print("  (none)")
        return
    keys = list(rows[0].keys())
    print("  " + " | ".join(keys))
    for row in rows:
        print("  " + " | ".join(str(row.get(key, "")) for key in keys))


def confirm_or_abort(stgs: list[str], args: argparse.Namespace) -> None:
    """Require explicit confirmation before deleting remote MySQL data."""
    if args.yes or args.dry_run:
        return
    print(f"Will purge MySQL v2 signal journal for stg={stgs}")
    answer = input("Type y to continue: ").strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("aborted")


def purge(conn, stgs: list[str]) -> tuple[int, int]:
    """Delete application checkpoints first, then signal events."""
    delete_apps = (
        text(
            """
            DELETE FROM strategy_signal_applications
            WHERE strategy_name IN :stgs
               OR signal_event_id IN (
                   SELECT id FROM trade_signal_events WHERE stg IN :stgs
               )
            """
        )
        .bindparams(bindparam("stgs", expanding=True))
    )
    delete_events = (
        text("DELETE FROM trade_signal_events WHERE stg IN :stgs")
        .bindparams(bindparam("stgs", expanding=True))
    )
    app_count = int(conn.execute(delete_apps, {"stgs": stgs}).rowcount or 0)
    event_count = int(conn.execute(delete_events, {"stgs": stgs}).rowcount or 0)
    return event_count, app_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge v2 MySQL trade_signal_events and strategy_signal_applications"
    )
    parser.add_argument(
        "--config",
        default=str(resolve_setting_path(DEFAULT_CONFIG)),
        help="Bridge/test config containing a mysql block.",
    )
    parser.add_argument("--stg", action="append", default=[], help="Strategy stg to purge.")
    parser.add_argument("--shadow-stg", action="append", default=[], help="Extra shadow stg.")
    parser.add_argument("--no-shadow", action="store_true", help="Do not append <stg>_shadow.")
    parser.add_argument("--dry-run", action="store_true", help="Only print matched rows.")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    setting_path = Path(args.config).resolve()
    setting = load_json(setting_path)
    stgs = resolve_stgs(setting, args)

    print(f"[config] {setting_path}")
    print(f"[stg] {stgs}")

    engine = mysql_engine(setting)
    try:
        with engine.begin() as conn:
            before_events, before_apps = fetch_summary(conn, stgs)
            print_summary("[before] trade_signal_events", before_events)
            print_summary("[before] strategy_signal_applications", before_apps)
            confirm_or_abort(stgs, args)
            if args.dry_run:
                print("[dry-run] no rows deleted")
                return
            event_count, app_count = purge(conn, stgs)
            print(f"[delete] trade_signal_events={event_count} strategy_signal_applications={app_count}")
            after_events, after_apps = fetch_summary(conn, stgs)
            print_summary("[after] trade_signal_events", after_events)
            print_summary("[after] strategy_signal_applications", after_apps)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
