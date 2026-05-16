"""Tests for the common strategy equity journal service."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from vnpy_common.persistence.strategy_equity_journal import (
    SOURCE_BROKER_LIVE_CLOSE,
    count_snapshots,
    list_snapshots,
)
from vnpy_common.persistence.strategy_trade_journal import record_trade
from vnpy_common.services.strategy_equity_journal_service import (
    BROKER_LIVE_EOD_JOURNAL_TIME_ENV,
    STRATEGY_INITIAL_CAPITALS_ENV,
    StrategyEquityJournalService,
)


class FakeMainEngine:
    def __init__(self) -> None:
        self.gateway = SimpleNamespace(td=SimpleNamespace())

    def get_gateway(self, gateway_name: str):
        return self.gateway

    def get_all_accounts(self):
        return [SimpleNamespace(gateway_name="QMT", balance=1_234_567.0)]

    def get_all_positions(self):
        return [
            SimpleNamespace(
                gateway_name="QMT",
                vt_symbol="510300.SSE",
                volume=100,
                price=12.0,
                pnl=0.0,
            ),
            SimpleNamespace(
                gateway_name="QMT",
                vt_symbol="159915.SZSE",
                volume=200,
                price=21.0,
                pnl=0.0,
            ),
            SimpleNamespace(gateway_name="QMT", volume=0),
        ]


def _clear_journal_cache(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    from vnpy_common.persistence import strategy_equity_journal, strategy_trade_journal

    strategy_equity_journal._init_done.clear()
    strategy_trade_journal._init_done.clear()
    return tmp_path / "state" / "strategy_equity_journal.db"


def _make_service(now: datetime) -> StrategyEquityJournalService:
    strategy = SimpleNamespace(
        strategy_name="live_strategy",
        gateway="QMT",
        inited=True,
        trading=True,
        get_variables=lambda: {"last_status": "ok"},
    )
    service = StrategyEquityJournalService(
        main_engine=FakeMainEngine(),
        is_trade_day=lambda day: True,
        now_provider=lambda: now,
    )
    service.register_provider(engine="SignalStrategyPlus", strategies={strategy.strategy_name: strategy})
    return service


def _make_multi_strategy_service(now: datetime) -> StrategyEquityJournalService:
    alpha = SimpleNamespace(
        strategy_name="alpha",
        gateway="QMT",
        inited=True,
        trading=True,
        get_variables=lambda: {},
    )
    beta = SimpleNamespace(
        strategy_name="beta",
        gateway="QMT",
        inited=True,
        trading=True,
        get_variables=lambda: {},
    )
    service = StrategyEquityJournalService(
        main_engine=FakeMainEngine(),
        is_trade_day=lambda day: True,
        now_provider=lambda: now,
    )
    service.register_provider(
        engine="SignalStrategyPlus",
        strategies={alpha.strategy_name: alpha, beta.strategy_name: beta},
    )
    return service


def test_broker_live_default_time_is_1600(monkeypatch, tmp_path: Path) -> None:
    db_path = _clear_journal_cache(monkeypatch, tmp_path)
    monkeypatch.delenv(BROKER_LIVE_EOD_JOURNAL_TIME_ENV, raising=False)

    service = _make_service(datetime(2026, 5, 15, 15, 30))
    service.persist_broker_live_eod_equity_after_close()
    assert count_snapshots(db_path=db_path) == 0

    service = _make_service(datetime(2026, 5, 15, 16, 0))
    service.persist_broker_live_eod_equity_after_close()
    assert count_snapshots(
        engine="SignalStrategyPlus",
        strategy_name="live_strategy",
        source_label=SOURCE_BROKER_LIVE_CLOSE,
        db_path=db_path,
    ) == 1


def test_broker_live_time_can_be_overridden_by_env(monkeypatch, tmp_path: Path) -> None:
    db_path = _clear_journal_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(BROKER_LIVE_EOD_JOURNAL_TIME_ENV, "15:30")

    service = _make_service(datetime(2026, 5, 15, 15, 29))
    service.persist_broker_live_eod_equity_after_close()
    assert count_snapshots(db_path=db_path) == 0

    service = _make_service(datetime(2026, 5, 15, 15, 30))
    service.persist_broker_live_eod_equity_after_close()
    assert count_snapshots(
        engine="SignalStrategyPlus",
        strategy_name="live_strategy",
        source_label=SOURCE_BROKER_LIVE_CLOSE,
        db_path=db_path,
    ) == 1


def test_broker_live_attributes_shared_account_by_strategy_trades(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = _clear_journal_cache(monkeypatch, tmp_path)
    monkeypatch.setenv(BROKER_LIVE_EOD_JOURNAL_TIME_ENV, "16:00")
    monkeypatch.setenv(
        STRATEGY_INITIAL_CAPITALS_ENV,
        '{"SignalStrategyPlus:alpha": 1000000, "SignalStrategyPlus:beta": 2000000}',
    )

    record_trade(
        gateway_name="QMT",
        tradeid="T1",
        orderid="O1",
        vt_symbol="510300.SSE",
        direction="多",
        offset="开",
        price=10.0,
        volume=100,
        datetime_value=datetime(2026, 5, 15, 9, 31),
        reference="alpha:1",
        db_path=db_path,
    )
    record_trade(
        gateway_name="QMT",
        tradeid="T2",
        orderid="O2",
        vt_symbol="159915.SZSE",
        direction="多",
        offset="开",
        price=20.0,
        volume=200,
        datetime_value=datetime(2026, 5, 15, 9, 32),
        reference="beta:1",
        db_path=db_path,
    )

    service = _make_multi_strategy_service(datetime(2026, 5, 15, 16, 0))
    service.persist_broker_live_eod_equity_after_close()

    rows = list_snapshots(source_label=SOURCE_BROKER_LIVE_CLOSE, db_path=db_path)
    by_strategy = {row["strategy_name"]: row for row in rows}

    assert by_strategy["alpha"]["strategy_value"] == 1_000_200.0
    assert by_strategy["beta"]["strategy_value"] == 2_000_200.0
    assert by_strategy["alpha"]["account_equity"] == 1_234_567.0
    assert by_strategy["beta"]["account_equity"] == 1_234_567.0
    assert by_strategy["alpha"]["raw_variables"]["attribution_method"] == "strategy_trade_journal"
    assert by_strategy["beta"]["raw_variables"]["attribution_method"] == "strategy_trade_journal"
