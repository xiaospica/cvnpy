from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from vnpy_qmt_sim.replay import SimReplayController


@dataclass
class FakePosition:
    vt_symbol: str
    volume: float = 100.0
    price: float = 10.0
    pnl: float = 0.0


class FakeMd:
    def __init__(self) -> None:
        self.refreshed: list[tuple[str, date]] = []

    def refresh_tick(self, vt_symbol: str, as_of_date: date) -> object:
        self.refreshed.append((vt_symbol, as_of_date))
        return object()


class FakeCounter:
    def __init__(self) -> None:
        self._replay_now = None
        self.capital = 1000.0
        self.frozen = 0.0
        self.positions = {
            "510300.SSE.long": FakePosition("510300.SSE"),
        }
        self.settled: list[date] = []

    def settle_end_of_day(self, settle_date: date) -> None:
        self.settled.append(settle_date)


class FakeTd:
    def __init__(self) -> None:
        self.counter = FakeCounter()


class FakeGateway:
    def __init__(self) -> None:
        self.md = FakeMd()
        self.td = FakeTd()
        self._auto_settle_enabled = True

    def enable_auto_settle(self, enabled: bool) -> None:
        self._auto_settle_enabled = enabled


class ExplicitAdapter:
    strategy_name = "demo"
    gateway_name = "QMT_SIM"

    def __init__(self) -> None:
        self.events: list[tuple[str, date | int]] = []

    def prepare(self, days: list[date]) -> None:
        self.events.append(("prepare", len(days)))

    def before_day(self, day: date) -> None:
        self.events.append(("before", day))

    def on_day_open(self, day: date) -> None:
        self.events.append(("open", day))

    def before_day_settle(self, day: date) -> None:
        self.events.append(("before_settle", day))

    def on_day_close(self, day: date) -> None:
        self.events.append(("close", day))

    def after_replay(self, end_day: date) -> None:
        self.events.append(("after", end_day))


def test_dynamic_signal_replay_settles_gap_days(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    db_path = tmp_path / "state" / "strategy_equity_journal.db"
    gateway = FakeGateway()
    controller = SimReplayController(
        gateway,
        engine="SignalStrategyPlus",
        strategy_name="etf_rotation_basic",
    )

    controller.on_external_signal_day(date(2026, 1, 2))
    controller.mark_signal_day(date(2026, 1, 2))
    controller.on_external_signal_day(date(2026, 1, 6))
    controller.mark_signal_day(date(2026, 1, 6))
    controller.finalize()

    assert gateway.td.counter.settled == [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]
    assert gateway._auto_settle_enabled is True
    assert gateway.td.counter._replay_now is None

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM strategy_equity_journal WHERE engine=? AND strategy_name=?",
            ("SignalStrategyPlus", "etf_rotation_basic"),
        ).fetchone()[0]
        labels = conn.execute(
            "SELECT DISTINCT source_label FROM strategy_equity_journal WHERE strategy_name=?",
            ("etf_rotation_basic",),
        ).fetchall()
    assert count == 3
    assert labels == [("replay_settle",)]


def test_explicit_replay_runs_adapter_hooks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    gateway = FakeGateway()
    adapter = ExplicitAdapter()
    controller = SimReplayController(
        gateway,
        engine="SignalStrategyPlus",
        strategy_name="demo",
    )

    controller.run_explicit(date(2026, 1, 2), date(2026, 1, 6), adapter)

    assert gateway.td.counter.settled == [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]
    assert adapter.events[0] == ("prepare", 3)
    assert adapter.events[-1] == ("after", date(2026, 1, 6))
    assert gateway._auto_settle_enabled is True
