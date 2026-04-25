from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from vnpy.trader.constant import Exchange
from vnpy.trader.object import SubscribeRequest

from vnpy_qmt_sim.bar_source import BarQuote, SimBarSource
from vnpy_qmt_sim.md import QmtSimMd


@dataclass
class _StubGateway:
    gateway_name: str = "QMT_SIM"

    def write_log(self, msg: str) -> None:
        return None


class _FixedSource(SimBarSource):
    name = "fixed"

    def __init__(self, quote: Optional[BarQuote]) -> None:
        self._quote = quote
        self.calls: list[tuple[str, date]] = []

    def get_quote(self, vt_symbol: str, as_of_date: date) -> Optional[BarQuote]:
        self.calls.append((vt_symbol, as_of_date))
        return self._quote


def _make_quote(vt_symbol: str, last: float) -> BarQuote:
    return BarQuote(
        vt_symbol=vt_symbol,
        as_of_date=date(2026, 4, 22),
        last_price=last,
        pre_close=last,
        open_price=last,
        high_price=last,
        low_price=last,
        limit_up=round(last * 1.1, 2),
        limit_down=round(last * 0.9, 2),
        pricetick=0.01,
        name="TEST",
    )


def test_subscribe_with_source_populates_real_tick() -> None:
    md = QmtSimMd(_StubGateway())  # type: ignore[arg-type]
    md.source = _FixedSource(_make_quote("000001.SZSE", 11.08))

    md.subscribe(SubscribeRequest(symbol="000001", exchange=Exchange.SZSE))

    tick = md.get_full_tick("000001.SZSE")
    assert tick is not None
    assert tick.last_price == 11.08
    assert tick.pre_close == 11.08
    assert tick.limit_up == 12.19
    assert tick.limit_down == 9.97
    assert tick.name == "TEST"


def test_subscribe_without_source_falls_back_to_synthetic() -> None:
    md = QmtSimMd(_StubGateway())  # type: ignore[arg-type]
    md.source = None

    md.subscribe(SubscribeRequest(symbol="000001", exchange=Exchange.SZSE))

    tick = md.get_full_tick("000001.SZSE")
    assert tick is not None
    assert tick.last_price == 10.0


def test_subscribe_with_missing_quote_falls_back_to_synthetic() -> None:
    md = QmtSimMd(_StubGateway())  # type: ignore[arg-type]
    md.source = _FixedSource(None)

    md.subscribe(SubscribeRequest(symbol="000001", exchange=Exchange.SZSE))

    tick = md.get_full_tick("000001.SZSE")
    assert tick is not None
    assert tick.last_price == 10.0  # synthetic fallback


def test_refresh_tick_can_override_previous_price() -> None:
    md = QmtSimMd(_StubGateway())  # type: ignore[arg-type]
    src = _FixedSource(_make_quote("000001.SZSE", 11.08))
    md.source = src
    md.subscribe(SubscribeRequest(symbol="000001", exchange=Exchange.SZSE))
    assert md.get_full_tick("000001.SZSE").last_price == 11.08

    # Swap source to new price, refresh explicitly.
    md.source = _FixedSource(_make_quote("000001.SZSE", 12.50))
    md.refresh_tick("000001.SZSE", as_of_date=date(2026, 4, 23))

    assert md.get_full_tick("000001.SZSE").last_price == 12.50
