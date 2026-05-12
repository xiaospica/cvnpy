"""SQLite event journal for webtrader WS/RPC events.

This is a lightweight fact source for small personal deployments. It stores
events before websocket fanout so mlearnweb can backfill after restart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnpy_common.data_paths import event_journal_db_path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_journal (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    topic         TEXT NOT NULL,
    node_id       TEXT,
    engine        TEXT,
    strategy_name TEXT,
    event_ts      REAL NOT NULL,
    data_json     TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_event_journal_topic_seq ON event_journal(topic, seq);
CREATE INDEX IF NOT EXISTS ix_event_journal_strategy_seq ON event_journal(strategy_name, seq);
"""

_lock = threading.Lock()
_init_done: set[str] = set()


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else event_journal_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    key = str(path)
    if key not in _init_done:
        with _lock:
            if key not in _init_done:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
                _init_done.add(key)
    return conn


def _extract_strategy_name(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("strategy_name", "strategy", "name"):
            value = data.get(key)
            if value:
                return str(value)
        reference = data.get("reference")
        if isinstance(reference, str) and ":" in reference:
            return reference.split(":", 1)[0]
    return ""


def append_event(*, topic: str, node_id: str, engine: str = "", data: Any = None, event_ts: Optional[float] = None, db_path: Optional[Path] = None) -> Optional[int]:
    payload = data if isinstance(data, dict) else {"value": data}
    event_ts = float(event_ts or time.time())
    event_id = f"{node_id}:{topic}:{event_ts:.6f}:{uuid.uuid4().hex[:10]}"
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = json.dumps({"repr": repr(payload)}, ensure_ascii=False)
    strategy_name = _extract_strategy_name(payload)
    try:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO event_journal
                    (event_id, topic, node_id, engine, strategy_name, event_ts, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, topic, node_id, engine, strategy_name, event_ts, raw),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()
    except Exception:
        return None


def list_events(*, since_seq: int = 0, limit: int = 1000, topic: str = "", strategy_name: str = "", db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 10000))
    sql = "SELECT seq, event_id, topic, node_id, engine, strategy_name, event_ts, data_json, created_at FROM event_journal WHERE seq > ?"
    args: list[Any] = [int(since_seq)]
    if topic:
        sql += " AND topic = ?"
        args.append(topic)
    if strategy_name:
        sql += " AND strategy_name = ?"
        args.append(strategy_name)
    sql += " ORDER BY seq ASC LIMIT ?"
    args.append(limit)
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row["data_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        item = dict(row)
        item["data"] = data
        item.pop("data_json", None)
        out.append(item)
    return out
