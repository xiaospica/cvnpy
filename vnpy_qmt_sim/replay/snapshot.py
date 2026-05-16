"""Replay equity snapshot helpers for QMT simulation replay."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy_common.persistence.strategy_equity_journal import (
    SOURCE_REPLAY_SETTLE,
    resolve_db_path,
    write_snapshot,
)


def resolve_strategy_equity_journal_db() -> Path:
    return resolve_db_path()


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
    engine: str,
    strategy_name: str,
    ts: datetime,
    strategy_value: float,
    account_equity: float,
    positions_count: int,
    raw_variables: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> bool:
    """UPSERT one replay-settle equity row into the common journal."""
    return write_snapshot(
        engine=engine,
        strategy_name=strategy_name,
        source_label=SOURCE_REPLAY_SETTLE,
        ts=ts,
        strategy_value=strategy_value,
        account_equity=account_equity,
        positions_count=positions_count,
        raw_variables=raw_variables,
        db_path=db_path,
    )
