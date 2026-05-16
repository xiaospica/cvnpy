"""SQLite strategy equity journal shared by all strategy engines."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnpy_common.data_paths import strategy_equity_journal_db_path


logger = logging.getLogger(__name__)

SOURCE_REPLAY_SETTLE = "replay_settle"
SOURCE_SIM_LIVE_SETTLE = "sim_live_settle"
SOURCE_BROKER_LIVE_CLOSE = "broker_live_close"

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS strategy_equity_journal (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,
    engine             TEXT    NOT NULL,
    strategy_name      TEXT    NOT NULL,
    source_label       TEXT    NOT NULL,
    ts                 TEXT    NOT NULL,
    strategy_value     REAL    NOT NULL,
    account_equity     REAL    NOT NULL,
    positions_count    INTEGER NOT NULL DEFAULT 0,
    raw_variables_json TEXT,
    inserted_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(engine, strategy_name, source_label, ts)
);
CREATE INDEX IF NOT EXISTS ix_strategy_equity_journal_identity_ts
    ON strategy_equity_journal(engine, strategy_name, source_label, ts);
CREATE INDEX IF NOT EXISTS ix_strategy_equity_journal_seq
    ON strategy_equity_journal(seq);
CREATE INDEX IF NOT EXISTS ix_strategy_equity_journal_ts
    ON strategy_equity_journal(ts);
"""

_lock = threading.Lock()
_init_done: set[str] = set()


def resolve_db_path() -> Path:
    return strategy_equity_journal_db_path()


def _get_conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    key = str(db_path)
    if key not in _init_done:
        with _lock:
            if key not in _init_done:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA_DDL)
                conn.commit()
                _init_done.add(key)
    return conn


def write_snapshot(
    *,
    engine: str,
    strategy_name: str,
    source_label: str,
    ts: datetime,
    strategy_value: float,
    account_equity: float,
    positions_count: int = 0,
    raw_variables: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """UPSERT one strategy equity fact.

    Persistence failures are logged and returned as ``False`` so trading and
    replay loops can continue.
    """
    path = db_path or resolve_db_path()
    try:
        conn = _get_conn(path)
        try:
            conn.execute(
                """
                INSERT INTO strategy_equity_journal
                    (engine, strategy_name, source_label, ts, strategy_value,
                     account_equity, positions_count, raw_variables_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(engine, strategy_name, source_label, ts) DO UPDATE SET
                    strategy_value = excluded.strategy_value,
                    account_equity = excluded.account_equity,
                    positions_count = excluded.positions_count,
                    raw_variables_json = excluded.raw_variables_json,
                    inserted_at = CURRENT_TIMESTAMP
                """,
                (
                    str(engine),
                    str(strategy_name),
                    str(source_label),
                    ts.isoformat() if isinstance(ts, datetime) else str(ts),
                    float(strategy_value),
                    float(account_equity),
                    int(positions_count),
                    json.dumps(raw_variables or {}, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning(
            "[strategy_equity_journal] write_snapshot(%s, %s, %s, %s) failed: %s",
            engine,
            strategy_name,
            source_label,
            ts,
            exc,
        )
        return False


def list_snapshots(
    *,
    engine: Optional[str] = None,
    strategy_name: Optional[str] = None,
    source_label: Optional[str] = None,
    since_ts: Optional[str] = None,
    since_seq: int = 0,
    limit: int = 10000,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List equity journal rows ordered by logical timestamp and sequence."""
    path = db_path or resolve_db_path()
    if not path.exists():
        return []

    limit = max(1, min(int(limit), 100000))
    sql = (
        "SELECT seq, engine, strategy_name, source_label, ts, strategy_value, "
        "       account_equity, positions_count, raw_variables_json, inserted_at "
        "FROM strategy_equity_journal WHERE seq > ?"
    )
    args: list[Any] = [int(since_seq or 0)]
    if engine:
        sql += " AND engine = ?"
        args.append(str(engine))
    if strategy_name:
        sql += " AND strategy_name = ?"
        args.append(str(strategy_name))
    if source_label:
        sql += " AND source_label = ?"
        args.append(str(source_label))
    if since_ts:
        sql += " AND datetime(ts) > datetime(?)"
        args.append(str(since_ts))
    sql += " ORDER BY datetime(ts) ASC, seq ASC LIMIT ?"
    args.append(limit)

    try:
        conn = _get_conn(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[strategy_equity_journal] list_snapshots failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            raw_variables = (
                json.loads(row["raw_variables_json"])
                if row["raw_variables_json"] else {}
            )
        except json.JSONDecodeError:
            raw_variables = {}
        out.append({
            "seq": int(row["seq"]),
            "engine": row["engine"],
            "strategy_name": row["strategy_name"],
            "source_label": row["source_label"],
            "ts": row["ts"],
            "strategy_value": float(row["strategy_value"]),
            "account_equity": float(row["account_equity"]),
            "positions_count": int(row["positions_count"] or 0),
            "raw_variables": raw_variables,
            "inserted_at": row["inserted_at"],
        })
    return out


def count_snapshots(
    *,
    engine: Optional[str] = None,
    strategy_name: Optional[str] = None,
    source_label: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    path = db_path or resolve_db_path()
    if not path.exists():
        return 0

    sql = "SELECT COUNT(*) FROM strategy_equity_journal WHERE 1=1"
    args: list[Any] = []
    if engine:
        sql += " AND engine = ?"
        args.append(str(engine))
    if strategy_name:
        sql += " AND strategy_name = ?"
        args.append(str(strategy_name))
    if source_label:
        sql += " AND source_label = ?"
        args.append(str(source_label))

    try:
        conn = _get_conn(path)
        try:
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("[strategy_equity_journal] count_snapshots failed: %s", exc)
        return 0


def delete_snapshots(
    *,
    engine: Optional[str] = None,
    strategy_name: Optional[str] = None,
    source_label: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Delete matching journal rows and return the affected row count."""
    path = db_path or resolve_db_path()
    if not path.exists():
        return 0

    sql = "DELETE FROM strategy_equity_journal WHERE 1=1"
    args: list[Any] = []
    if engine:
        sql += " AND engine = ?"
        args.append(str(engine))
    if strategy_name:
        sql += " AND strategy_name = ?"
        args.append(str(strategy_name))
    if source_label:
        sql += " AND source_label = ?"
        args.append(str(source_label))

    try:
        conn = _get_conn(path)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[strategy_equity_journal] delete_snapshots failed: %s", exc)
        return 0
