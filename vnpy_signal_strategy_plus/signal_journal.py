"""Signal journal tables and helpers for strategy simulation rebuild.

The legacy MySQL signal table is no longer part of the canonical signal path. JoinQuant
signals are normalized into ``trade_signal_events`` and each strategy/account
records its own consumption checkpoint in ``strategy_signal_applications``.

Important field contract inherited from JoinQuant:
``pct`` means *this trade value / total portfolio value*.  It is not target
weight, not percent of available cash, and not percent of current holding.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Session, declarative_base


SignalJournalBase = declarative_base()

PCT_SEMANTICS = "trade_value_pct_of_total_portfolio"


class TradeSignalEvent(SignalJournalBase):
    """Normalized append-only signal fact."""

    __tablename__ = "trade_signal_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_uid = Column(String(160), nullable=False, unique=True, index=True)
    source = Column(String(32), nullable=False, default="joinquant")
    source_signal_id = Column(String(160), nullable=True)
    stream_key = Column(String(128), nullable=True)
    redis_id = Column(String(64), nullable=True)

    stg = Column(String(64), nullable=False, index=True)
    code = Column(String(32), nullable=False)
    signal_type = Column(String(32), nullable=False)
    pct = Column(Float, nullable=False)
    pct_semantics = Column(String(80), nullable=False, default=PCT_SEMANTICS)
    price = Column(Float, nullable=False, default=0.0)
    empty = Column(Boolean, nullable=False, default=False)
    amt = Column(Float, nullable=True)
    remark = Column(DateTime, nullable=False)
    trade_date = Column(String(10), nullable=False, index=True)

    payload_hash = Column(String(64), nullable=False)
    raw_payload = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_trade_signal_events_stg_remark", "stg", "remark"),
    )

    @property
    def type(self) -> str:
        """Compatibility inside strategy code: order logic expects ``signal.type``."""
        return str(self.signal_type)


class StrategySignalApplication(SignalJournalBase):
    """Per strategy/account checkpoint for consumed signal events."""

    __tablename__ = "strategy_signal_applications"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "gateway_name",
            "engine",
            "strategy_name",
            "signal_event_id",
            name="uq_strategy_signal_application",
        ),
        Index(
            "ix_strategy_signal_applications_scope",
            "account_id",
            "gateway_name",
            "engine",
            "strategy_name",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_event_id = Column(
        Integer,
        ForeignKey("trade_signal_events.id"),
        nullable=False,
    )
    account_id = Column(String(64), nullable=False)
    gateway_name = Column(String(64), nullable=False)
    engine = Column(String(64), nullable=False)
    strategy_name = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="applied")
    order_refs_json = Column(Text, nullable=True)
    error_msg = Column(Text, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


def parse_bool(value: Any) -> bool:
    """Parse Redis/MySQL style truthy values."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _parse_remark(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("remark is empty")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(text)


def json_dumps(value: Any) -> str:
    """Stable JSON representation used for raw payloads and order refs."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    """Return a stable SHA256 for the raw source payload."""
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def normalize_trade_signal_payload(
    payload: dict[str, Any],
    *,
    target_stg: str,
    stream_key: str | None = None,
    redis_id: str | None = None,
    source: str = "joinquant",
) -> dict[str, Any]:
    """Normalize a JoinQuant signal payload without changing its semantics."""
    code = str(payload["code"])
    signal_type = str(payload["type"])
    stg = str(payload.get("stg") or target_stg)
    remark = _parse_remark(payload["remark"])
    raw_hash = payload_hash(payload)

    explicit_uid = payload.get("signal_uid")
    source_signal_id = payload.get("source_signal_id") or payload.get("id")
    if explicit_uid:
        signal_uid = str(explicit_uid)
    elif source_signal_id:
        signal_uid = f"{source}:source:{source_signal_id}"
    elif redis_id:
        signal_uid = f"{source}:redis:{stream_key or stg}:{redis_id}"
    else:
        signal_uid = f"{source}:hash:{raw_hash[:32]}"

    pct_semantics = str(payload.get("pct_semantics") or PCT_SEMANTICS)
    if pct_semantics != PCT_SEMANTICS:
        raise ValueError(
            f"unsupported pct_semantics={pct_semantics!r}; expected {PCT_SEMANTICS!r}"
        )

    amt_raw = payload.get("amt")
    return {
        "signal_uid": signal_uid,
        "source": source,
        "source_signal_id": str(source_signal_id) if source_signal_id else None,
        "stream_key": str(stream_key) if stream_key else None,
        "redis_id": str(redis_id) if redis_id else None,
        "stg": stg,
        "code": code,
        "signal_type": signal_type,
        "pct": float(payload.get("pct", 0) or 0),
        "pct_semantics": PCT_SEMANTICS,
        "price": float(payload.get("price", 0) or 0),
        "empty": parse_bool(payload.get("empty", "0")),
        "amt": float(amt_raw) if amt_raw not in (None, "") else None,
        "remark": remark,
        "trade_date": remark.date().isoformat(),
        "payload_hash": raw_hash,
        "raw_payload": json_dumps(payload),
    }


def upsert_trade_signal_event(
    session: Session,
    normalized: dict[str, Any],
) -> tuple[TradeSignalEvent, bool]:
    """Insert a signal event once and return ``(row, created)``."""
    row = (
        session.query(TradeSignalEvent)
        .filter(TradeSignalEvent.signal_uid == normalized["signal_uid"])
        .one_or_none()
    )
    if row is not None:
        return row, False

    row = TradeSignalEvent(**normalized)
    session.add(row)
    session.flush()
    return row, True


def record_signal_application(
    session: Session,
    *,
    signal_event_id: int,
    account_id: str,
    gateway_name: str,
    engine: str,
    strategy_name: str,
    status: str,
    order_refs: Sequence[str] | None = None,
    error_msg: str | None = None,
) -> StrategySignalApplication:
    """Insert or update the per-strategy consumption checkpoint."""
    row = (
        session.query(StrategySignalApplication)
        .filter(
            StrategySignalApplication.account_id == account_id,
            StrategySignalApplication.gateway_name == gateway_name,
            StrategySignalApplication.engine == engine,
            StrategySignalApplication.strategy_name == strategy_name,
            StrategySignalApplication.signal_event_id == signal_event_id,
        )
        .one_or_none()
    )
    if row is None:
        row = StrategySignalApplication(
            signal_event_id=signal_event_id,
            account_id=account_id,
            gateway_name=gateway_name,
            engine=engine,
            strategy_name=strategy_name,
        )
        session.add(row)

    row.status = status
    row.order_refs_json = json_dumps(list(order_refs or []))
    row.error_msg = error_msg
    row.applied_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    session.flush()
    return row
