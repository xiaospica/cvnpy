"""Unit tests for the common strategy equity journal."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from vnpy_common.persistence.strategy_equity_journal import (
    SOURCE_REPLAY_SETTLE,
    SOURCE_SIM_LIVE_SETTLE,
    count_snapshots,
    list_snapshots,
    resolve_db_path,
    write_snapshot,
)


def test_write_and_list_basic(tmp_path: Path) -> None:
    db_path = tmp_path / "strategy_equity_journal.db"
    ts = datetime(2026, 1, 5, 15, 0, 0)
    ok = write_snapshot(
        engine="MlStrategy",
        strategy_name="csi300_a",
        source_label=SOURCE_REPLAY_SETTLE,
        ts=ts,
        strategy_value=1_000_000.0,
        account_equity=1_000_000.0,
        positions_count=7,
        db_path=db_path,
    )
    assert ok is True
    assert db_path.exists()

    rows = list_snapshots(
        engine="MlStrategy",
        strategy_name="csi300_a",
        source_label=SOURCE_REPLAY_SETTLE,
        db_path=db_path,
    )
    assert len(rows) == 1
    assert rows[0]["engine"] == "MlStrategy"
    assert rows[0]["strategy_name"] == "csi300_a"
    assert rows[0]["source_label"] == SOURCE_REPLAY_SETTLE
    assert rows[0]["ts"] == ts.isoformat()
    assert rows[0]["strategy_value"] == 1_000_000.0
    assert rows[0]["positions_count"] == 7


def test_upsert_identity_includes_engine_and_source(tmp_path: Path) -> None:
    db_path = tmp_path / "strategy_equity_journal.db"
    ts = datetime(2026, 1, 5, 15, 0, 0)
    write_snapshot(
        engine="SignalStrategyPlus",
        strategy_name="demo",
        source_label=SOURCE_SIM_LIVE_SETTLE,
        ts=ts,
        strategy_value=1_000_000.0,
        account_equity=1_000_000.0,
        db_path=db_path,
    )
    write_snapshot(
        engine="SignalStrategyPlus",
        strategy_name="demo",
        source_label=SOURCE_SIM_LIVE_SETTLE,
        ts=ts,
        strategy_value=1_050_000.0,
        account_equity=1_050_000.0,
        positions_count=5,
        db_path=db_path,
    )
    write_snapshot(
        engine="MlStrategy",
        strategy_name="demo",
        source_label=SOURCE_REPLAY_SETTLE,
        ts=ts,
        strategy_value=2_000_000.0,
        account_equity=2_000_000.0,
        db_path=db_path,
    )

    assert count_snapshots(db_path=db_path) == 2
    rows = list_snapshots(
        engine="SignalStrategyPlus",
        strategy_name="demo",
        source_label=SOURCE_SIM_LIVE_SETTLE,
        db_path=db_path,
    )
    assert len(rows) == 1
    assert rows[0]["strategy_value"] == 1_050_000.0
    assert rows[0]["positions_count"] == 5


def test_list_filters_and_orders(tmp_path: Path) -> None:
    db_path = tmp_path / "strategy_equity_journal.db"
    base = datetime(2026, 1, 5, 15, 0, 0)
    for offset in (3, 1, 2):
        write_snapshot(
            engine="MlStrategy",
            strategy_name="csi300_a",
            source_label=SOURCE_REPLAY_SETTLE,
            ts=base + timedelta(days=offset),
            strategy_value=1_000_000.0 + offset,
            account_equity=1_000_000.0 + offset,
            db_path=db_path,
        )
    write_snapshot(
        engine="SignalStrategyPlus",
        strategy_name="csi300_a",
        source_label=SOURCE_REPLAY_SETTLE,
        ts=base,
        strategy_value=2.0,
        account_equity=2.0,
        db_path=db_path,
    )

    rows = list_snapshots(
        engine="MlStrategy",
        strategy_name="csi300_a",
        source_label=SOURCE_REPLAY_SETTLE,
        since_ts=(base + timedelta(days=1)).isoformat(),
        db_path=db_path,
    )
    assert [row["strategy_value"] for row in rows] == [
        1_000_002.0,
        1_000_003.0,
    ]


def test_list_empty_when_db_not_exist(tmp_path: Path) -> None:
    rows = list_snapshots(db_path=tmp_path / "missing.db")
    assert rows == []


def test_resolve_db_path_uses_vnpy_data_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    expected = tmp_path / "state" / "strategy_equity_journal.db"
    assert resolve_db_path() == expected
