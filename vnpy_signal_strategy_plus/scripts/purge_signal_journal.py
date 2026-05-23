# -*- coding: utf-8 -*-
"""Purge SignalStrategyPlus v2 signal journal state by strategy.

This script intentionally touches only the canonical v2 MySQL tables
(``trade_signal_events`` and ``strategy_signal_applications``) plus the
configured Redis Stream backlog.  It never deletes the legacy ``stock_trade``
table.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import redis
except ImportError:  # pragma: no cover - reported at runtime when Redis purge is requested.
    redis = None  # type: ignore[assignment]

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


def resolve_runner_id(setting: dict[str, Any], args: argparse.Namespace) -> str:
    """Resolve runner_id used by runner-scoped shadow strategy names."""
    if args.runner_id:
        return str(args.runner_id).strip()
    for key in ("runner_id", "signal_runner_id"):
        if setting.get(key):
            return str(setting[key]).strip()
    dual_track = setting.get("dual_track")
    if isinstance(dual_track, dict) and dual_track.get("runner_id"):
        return str(dual_track["runner_id"]).strip()
    return ""


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
            runner_id = resolve_runner_id(setting, args)
            resolved.extend(f"{name}_shadow" for name in names)
            if runner_id:
                resolved.extend(f"{name}_shadow_{runner_id}" for name in names)
    return unique(resolved)


def resolve_streams(setting: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Resolve Redis stream names from bridge/runtime config plus CLI overrides."""
    streams = list(args.stream or [])
    redis_cfg = setting.get("redis")
    if isinstance(redis_cfg, dict) and redis_cfg.get("stream_key"):
        streams.append(str(redis_cfg["stream_key"]))
    for sub in setting.get("subscriptions") or []:
        if isinstance(sub, dict) and sub.get("stream_key"):
            streams.append(str(sub["stream_key"]))
    return unique(streams)


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


def redis_client(setting: dict[str, Any]):
    """Create a Redis client from the config redis block."""
    if redis is None:
        raise SystemExit("redis package is not installed; install it or pass --skip-redis")
    cfg = setting.get("redis")
    if not isinstance(cfg, dict):
        raise SystemExit("config has no redis block; pass --skip-redis or provide --stream with redis config")
    return redis.Redis(
        host=str(cfg["host"]),
        port=int(cfg.get("port") or 6379),
        password=cfg.get("password") or None,
        db=int(cfg.get("db") or 0),
        decode_responses=True,
    )


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


def fetch_redis_summary(client, streams: list[str]) -> list[dict[str, Any]]:
    """Return compact Redis Stream state for safety checks."""
    rows: list[dict[str, Any]] = []
    for stream in streams:
        exists = int(client.exists(stream))
        xlen = int(client.xlen(stream)) if exists else 0
        group_names: list[str] = []
        pending_total = 0
        if exists:
            try:
                groups = client.xinfo_groups(stream)
                for group in groups:
                    name = group.get("name", "")
                    if isinstance(name, bytes):
                        name = name.decode("utf-8", errors="replace")
                    group_names.append(str(name))
                    pending_total += int(group.get("pending") or 0)
            except Exception as exc:  # Redis may raise when the key is not a stream.
                group_names.append(f"<xinfo_groups_error:{exc}>")

        sample_count = min(xlen, 10000)
        with_signal_uid = 0
        without_signal_uid = 0
        if sample_count:
            try:
                for _, payload in client.xrange(stream, count=sample_count):
                    if payload.get("signal_uid"):
                        with_signal_uid += 1
                    else:
                        without_signal_uid += 1
            except Exception:
                with_signal_uid = -1
                without_signal_uid = -1

        rows.append(
            {
                "stream": stream,
                "exists": exists,
                "xlen": xlen,
                "groups": ",".join(group_names) if group_names else "",
                "pending": pending_total,
                "sample_with_uid": with_signal_uid,
                "sample_without_uid": without_signal_uid,
            }
        )
    return rows


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


def confirm_or_abort(stgs: list[str], streams: list[str], args: argparse.Namespace) -> None:
    """Require explicit confirmation before deleting remote signal state."""
    if args.yes or args.dry_run:
        return
    print(f"Will purge MySQL v2 signal journal for stg={stgs}")
    if not args.skip_redis and streams:
        print(f"Will purge Redis streams by {args.redis_mode}: streams={streams}")
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


def purge_redis_streams(client, streams: list[str], mode: str) -> list[dict[str, Any]]:
    """Delete or trim Redis Stream backlog and consumer groups."""
    results: list[dict[str, Any]] = []
    for stream in streams:
        if mode == "delete":
            deleted = int(client.delete(stream) or 0)
            results.append({"stream": stream, "action": "DEL", "deleted": deleted})
            continue

        destroyed_groups = 0
        try:
            for group in client.xinfo_groups(stream):
                group_name = group.get("name", "")
                destroyed_groups += int(client.xgroup_destroy(stream, group_name) or 0)
        except Exception:
            destroyed_groups = 0
        try:
            trimmed = int(client.xtrim(stream, maxlen=0, approximate=False) or 0)
        except Exception as exc:
            results.append(
                {
                    "stream": stream,
                    "action": "XTRIM",
                    "trimmed": 0,
                    "destroyed_groups": destroyed_groups,
                    "error": str(exc),
                }
            )
            continue
        results.append(
            {
                "stream": stream,
                "action": "XTRIM",
                "trimmed": trimmed,
                "destroyed_groups": destroyed_groups,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge v2 MySQL signal journal rows and Redis Stream backlog"
    )
    parser.add_argument(
        "--config",
        default=str(resolve_setting_path(DEFAULT_CONFIG)),
        help="Bridge/test config containing mysql and redis blocks.",
    )
    parser.add_argument("--stg", action="append", default=[], help="Strategy stg to purge.")
    parser.add_argument("--shadow-stg", action="append", default=[], help="Extra shadow stg.")
    parser.add_argument("--runner-id", default="", help="Append <stg>_shadow_<runner_id> when set.")
    parser.add_argument("--no-shadow", action="store_true", help="Do not append <stg>_shadow.")
    parser.add_argument("--stream", action="append", default=[], help="Extra Redis stream to purge.")
    parser.add_argument("--skip-redis", action="store_true", help="Only purge MySQL rows.")
    parser.add_argument(
        "--redis-mode",
        choices=["delete", "trim"],
        default="delete",
        help="Redis cleanup mode. delete removes stream key and consumer groups; trim keeps the key.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only print matched rows.")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    setting_path = Path(args.config).resolve()
    setting = load_json(setting_path)
    stgs = resolve_stgs(setting, args)
    streams = resolve_streams(setting, args)

    print(f"[config] {setting_path}")
    print(f"[stg] {stgs}")
    if not args.skip_redis:
        print(f"[redis] mode={args.redis_mode} streams={streams}")

    rds = None
    if not args.skip_redis:
        if not streams:
            raise SystemExit("no Redis stream resolved; pass --stream or --skip-redis")
        rds = redis_client(setting)

    engine = mysql_engine(setting)
    try:
        with engine.begin() as conn:
            before_events, before_apps = fetch_summary(conn, stgs)
            print_summary("[before] trade_signal_events", before_events)
            print_summary("[before] strategy_signal_applications", before_apps)
            if rds is not None:
                print_summary("[before] redis_streams", fetch_redis_summary(rds, streams))
            confirm_or_abort(stgs, streams, args)
            if args.dry_run:
                print("[dry-run] no rows deleted")
                return
            event_count, app_count = purge(conn, stgs)
            print(f"[delete] trade_signal_events={event_count} strategy_signal_applications={app_count}")
            if rds is not None:
                print_summary("[delete] redis_streams", purge_redis_streams(rds, streams, args.redis_mode))
            after_events, after_apps = fetch_summary(conn, stgs)
            print_summary("[after] trade_signal_events", after_events)
            print_summary("[after] strategy_signal_applications", after_apps)
            if rds is not None:
                print_summary("[after] redis_streams", fetch_redis_summary(rds, streams))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
