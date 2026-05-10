"""Replay equity snapshot persistence.

This module intentionally lives in ``vnpy_qmt_sim`` so the simulator can write
replay equity without importing ``vnpy_ml_strategy`` or signal-strategy code.
The schema is compatible with ``vnpy_ml_strategy.replay_history``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS replay_equity_snapshots (
    strategy_name      TEXT    NOT NULL,
    ts                 TEXT    NOT NULL,
    strategy_value     REAL    NOT NULL,
    account_equity     REAL    NOT NULL,
    positions_count    INTEGER NOT NULL DEFAULT 0,
    raw_variables_json TEXT,
    inserted_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_name, ts)
);
CREATE INDEX IF NOT EXISTS idx_inserted_at ON replay_equity_snapshots (inserted_at);
CREATE INDEX IF NOT EXISTS idx_strategy_ts ON replay_equity_snapshots (strategy_name, ts);
"""

_lock = threading.Lock()
_init_done: dict[str, bool] = {}


def resolve_replay_history_db() -> Path:
    """Resolve the local replay-history SQLite path."""
    explicit = os.environ.get("REPLAY_HISTORY_DB")
    if explicit:
        return Path(explicit)
    qs_root = os.environ.get("QS_DATA_ROOT", r"D:/vnpy_data")
    return Path(qs_root) / "state" / "replay_history.db"


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection and initialize the snapshot schema once."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    key = str(db_path)
    if not _init_done.get(key):
        with _lock:
            if not _init_done.get(key):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA_DDL)
                conn.commit()
                _init_done[key] = True
    return conn


def calculate_gateway_equity(gateway: Any) -> tuple[float, int]:
    """Return ``(equity, positions_count)`` from a simulation gateway."""
    counter = gateway.td.counter
    cash = float(counter.capital - counter.frozen)
    market_value = 0.0
    positions_count = 0

    for pos in counter.positions.values():
        volume = float(getattr(pos, "volume", 0) or 0)
        if volume <= 0:
            continue
        price = float(getattr(pos, "price", 0) or 0)
        pnl = float(getattr(pos, "pnl", 0) or 0)
        market_value += volume * price + pnl
        positions_count += 1

    return cash + market_value, positions_count


def write_replay_snapshot(
    *,
    strategy_name: str,
    ts: datetime,
    strategy_value: float,
    account_equity: float,
    positions_count: int,
    raw_variables: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> bool:
    """UPSERT one replay-equity snapshot row.

    The function does not raise on persistence errors; callers can keep replay
    moving and inspect warnings in logs.
    """
    path = db_path or resolve_replay_history_db()
    try:
        conn = _get_conn(path)
        try:
            conn.execute(
                """
                INSERT INTO replay_equity_snapshots
                    (strategy_name, ts, strategy_value, account_equity,
                     positions_count, raw_variables_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, ts) DO UPDATE SET
                    strategy_value = excluded.strategy_value,
                    account_equity = excluded.account_equity,
                    positions_count = excluded.positions_count,
                    raw_variables_json = excluded.raw_variables_json,
                    inserted_at = CURRENT_TIMESTAMP
                """,
                (
                    strategy_name,
                    ts.isoformat(),
                    float(strategy_value),
                    float(account_equity),
                    int(positions_count),
                    json.dumps(raw_variables or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning(
            "[replay_snapshot] write_replay_snapshot(%s, %s) failed: %s",
            strategy_name,
            ts,
            exc,
        )
        return False
