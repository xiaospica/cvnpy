"""Strategy trade attribution ledger for broker-live accounts.

The real broker account is account-level, while strategy ownership lives in
``OrderRequest.reference`` (``{strategy_name}:{seq}``).  QMT uses its own
``order_remark`` as the broker-facing order id, so we persist the local mapping
and the resulting fills in the same SQLite file as strategy equity journal.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnpy_common.data_paths import strategy_equity_journal_db_path


logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS strategy_order_refs (
    gateway_name  TEXT NOT NULL,
    orderid       TEXT NOT NULL,
    vt_symbol     TEXT,
    reference     TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (gateway_name, orderid)
);
CREATE INDEX IF NOT EXISTS ix_strategy_order_refs_strategy
    ON strategy_order_refs(gateway_name, strategy_name);

CREATE TABLE IF NOT EXISTS strategy_trade_journal (
    gateway_name  TEXT NOT NULL,
    tradeid       TEXT NOT NULL,
    orderid       TEXT NOT NULL,
    vt_symbol     TEXT NOT NULL,
    direction     TEXT NOT NULL,
    offset        TEXT,
    price         REAL NOT NULL,
    volume        REAL NOT NULL,
    datetime      TEXT NOT NULL,
    reference     TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    inserted_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (gateway_name, tradeid)
);
CREATE INDEX IF NOT EXISTS ix_strategy_trade_journal_strategy_dt
    ON strategy_trade_journal(gateway_name, strategy_name, datetime);
CREATE INDEX IF NOT EXISTS ix_strategy_trade_journal_order
    ON strategy_trade_journal(gateway_name, orderid);
"""

_lock = threading.Lock()
_init_done: set[str] = set()


def resolve_db_path() -> Path:
    return strategy_equity_journal_db_path()


def parse_strategy_name(reference: str) -> str:
    """Return the strategy name from ``{strategy_name}:{seq}`` references."""
    text = str(reference or "").strip()
    if not text or ":" not in text:
        return ""
    return text.split(":", 1)[0]


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


def record_order_reference(
    *,
    gateway_name: str,
    orderid: str,
    reference: str,
    vt_symbol: str = "",
    db_path: Optional[Path] = None,
) -> bool:
    """Persist the broker order id to strategy reference mapping."""
    strategy_name = parse_strategy_name(reference)
    if not gateway_name or not orderid or not reference or not strategy_name:
        return False
    path = db_path or resolve_db_path()
    try:
        conn = _get_conn(path)
        try:
            conn.execute(
                """
                INSERT INTO strategy_order_refs
                    (gateway_name, orderid, vt_symbol, reference, strategy_name)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(gateway_name, orderid) DO UPDATE SET
                    vt_symbol = excluded.vt_symbol,
                    reference = excluded.reference,
                    strategy_name = excluded.strategy_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (gateway_name, orderid, vt_symbol, reference, strategy_name),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning("[strategy_trade_journal] record_order_reference failed: %s", exc)
        return False


def get_order_reference(
    *,
    gateway_name: str,
    orderid: str,
    db_path: Optional[Path] = None,
) -> str:
    """Look up a persisted strategy reference for a broker order id."""
    path = db_path or resolve_db_path()
    if not path.exists():
        return ""
    try:
        conn = _get_conn(path)
        try:
            row = conn.execute(
                """
                SELECT reference FROM strategy_order_refs
                WHERE gateway_name = ? AND orderid = ?
                """,
                (gateway_name, orderid),
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else ""
    except Exception as exc:
        logger.warning("[strategy_trade_journal] get_order_reference failed: %s", exc)
        return ""


def record_trade(
    *,
    gateway_name: str,
    tradeid: str,
    orderid: str,
    vt_symbol: str,
    direction: str,
    price: float,
    volume: float,
    datetime_value: datetime | str,
    reference: str = "",
    offset: str = "",
    db_path: Optional[Path] = None,
) -> bool:
    """Persist an attributed trade fill."""
    reference = reference or get_order_reference(gateway_name=gateway_name, orderid=orderid, db_path=db_path)
    strategy_name = parse_strategy_name(reference)
    if not all([gateway_name, tradeid, orderid, vt_symbol, direction, reference, strategy_name]):
        return False
    path = db_path or resolve_db_path()
    dt_text = (
        datetime_value.isoformat()
        if isinstance(datetime_value, datetime) else str(datetime_value)
    )
    try:
        conn = _get_conn(path)
        try:
            conn.execute(
                """
                INSERT INTO strategy_trade_journal
                    (gateway_name, tradeid, orderid, vt_symbol, direction, offset,
                     price, volume, datetime, reference, strategy_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gateway_name, tradeid) DO UPDATE SET
                    orderid = excluded.orderid,
                    vt_symbol = excluded.vt_symbol,
                    direction = excluded.direction,
                    offset = excluded.offset,
                    price = excluded.price,
                    volume = excluded.volume,
                    datetime = excluded.datetime,
                    reference = excluded.reference,
                    strategy_name = excluded.strategy_name
                """,
                (
                    gateway_name,
                    tradeid,
                    orderid,
                    vt_symbol,
                    direction,
                    offset,
                    float(price),
                    float(volume),
                    dt_text,
                    reference,
                    strategy_name,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning("[strategy_trade_journal] record_trade failed: %s", exc)
        return False


def list_strategy_trades(
    *,
    gateway_name: str,
    strategy_name: str,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List persisted trades for one strategy ordered by fill time."""
    path = db_path or resolve_db_path()
    if not path.exists():
        return []
    try:
        conn = _get_conn(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT gateway_name, tradeid, orderid, vt_symbol, direction, offset,
                       price, volume, datetime, reference, strategy_name
                FROM strategy_trade_journal
                WHERE gateway_name = ? AND strategy_name = ?
                ORDER BY datetime ASC, tradeid ASC
                """,
                (gateway_name, strategy_name),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[strategy_trade_journal] list_strategy_trades failed: %s", exc)
        return []

    return [dict(row) for row in rows]
