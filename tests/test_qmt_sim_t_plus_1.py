from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pytest

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import (
    AccountData,
    OrderData,
    OrderRequest,
    PositionData,
    TickData,
    TradeData,
)

from vnpy_qmt_sim.bar_source.base import BarQuote
from vnpy_qmt_sim.td import SimulationCounter


class _DummyMd:
    def __init__(self, tick: Optional[TickData] = None, quote: Optional[BarQuote] = None):
        self._tick = tick
        self._quote = quote

    def get_full_tick(self, vt_symbol: str) -> Optional[TickData]:
        if self._tick and self._tick.vt_symbol == vt_symbol:
            return self._tick
        return None

    def get_quote(self, vt_symbol: str) -> Optional[BarQuote]:
        if self._quote and self._quote.vt_symbol == vt_symbol:
            return self._quote
        return None


class _DummyGateway:
    gateway_name = "QMT_SIM"

    def __init__(self, md: _DummyMd):
        self.md = md
        self.events: list[tuple[str, object]] = []

    def on_order(self, order: OrderData) -> None:
        self.events.append(("order", order))

    def on_trade(self, trade: TradeData) -> None:
        self.events.append(("trade", trade))

    def on_account(self, account: AccountData) -> None:
        self.events.append(("account", account))

    def on_position(self, position: PositionData) -> None:
        self.events.append(("position", position))

    def write_log(self, msg: str) -> None:
        return None


def _tick(vt_symbol: str, last: float = 11.0) -> TickData:
    sym, ex = vt_symbol.split(".")
    return TickData(
        gateway_name="QMT_SIM",
        symbol=sym,
        exchange=Exchange(ex),
        datetime=datetime.now(),
        last_price=last,
        limit_up=last * 1.10,
        limit_down=last * 0.90,
        bid_price_1=last - 0.01,
        ask_price_1=last + 0.01,
    )


def _quote(vt_symbol: str, pct_chg: float = 0.0, last: float = 11.0) -> BarQuote:
    return BarQuote(
        vt_symbol=vt_symbol,
        as_of_date=date(2026, 4, 22),
        last_price=last,
        pre_close=last / (1 + pct_chg / 100.0) if pct_chg != 0 else last,
        open_price=last,
        high_price=last,
        low_price=last,
        limit_up=last * 1.10,
        limit_down=last * 0.90,
        pricetick=0.01,
        name="TEST",
        pct_chg=pct_chg,
    )


def _orderid(vt_orderid: str) -> str:
    """vt_orderid 形如 'QMT_SIM.1'，counter.orders 按 'orderid' 索引。"""
    return vt_orderid.rsplit(".", 1)[-1]


def _buy_then_fill(counter: SimulationCounter, vt_symbol: str, volume: int, price: float) -> str:
    sym, ex = vt_symbol.split(".")
    req = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=price, volume=volume, reference="",
    )
    return _orderid(counter.send_order(req))


def _sell(counter: SimulationCounter, vt_symbol: str, volume: int, price: float) -> str:
    sym, ex = vt_symbol.split(".")
    req = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.SHORT, type=OrderType.LIMIT,
        offset=Offset.CLOSE, price=price, volume=volume, reference="",
    )
    return _orderid(counter.send_order(req))


def test_buy_today_cannot_sell_same_day() -> None:
    """T+1：当日买入的股票，当日不可卖出。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    _buy_then_fill(counter, vt, volume=200, price=11.0)
    pos_key = f"{vt}.{Direction.LONG.value}"
    pos = counter.positions[pos_key]
    assert pos.volume == 200
    assert pos.yd_volume == 0  # 今日买入不计入昨仓

    sell_id = _sell(counter, vt, volume=200, price=11.0)
    sell_order = counter.orders[sell_id]
    assert sell_order.status == Status.REJECTED
    assert "T+1" in sell_order.status_msg or "可用持仓不足" in sell_order.status_msg


def test_settle_end_of_day_makes_position_sellable() -> None:
    """日终结算后，今日买入的股票转为可卖。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0), quote=_quote(vt, pct_chg=0.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    _buy_then_fill(counter, vt, volume=200, price=11.0)
    counter.settle_end_of_day(date(2026, 4, 22))

    pos_key = f"{vt}.{Direction.LONG.value}"
    pos = counter.positions[pos_key]
    assert pos.volume == 200
    assert pos.yd_volume == 200  # 已转为可卖

    sell_id = _sell(counter, vt, volume=200, price=11.0)
    assert counter.orders[sell_id].status != Status.REJECTED


def test_settle_marks_price_with_pct_chg() -> None:
    """settle_end_of_day 用 pct_chg 累乘 pos.price，模拟除权连续性。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0), quote=_quote(vt, pct_chg=2.0, last=11.22))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    _buy_then_fill(counter, vt, volume=200, price=11.0)
    pos = counter.positions[f"{vt}.{Direction.LONG.value}"]
    assert pos.price == pytest.approx(11.0)

    counter.settle_end_of_day(date(2026, 4, 22))
    assert pos.price == pytest.approx(11.0 * 1.02)


def test_settle_idempotent_for_same_date() -> None:
    """同一日期重复 settle 不应叠加 mark price。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0), quote=_quote(vt, pct_chg=2.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    _buy_then_fill(counter, vt, volume=200, price=11.0)
    counter.settle_end_of_day(date(2026, 4, 22))
    counter.settle_end_of_day(date(2026, 4, 22))

    pos = counter.positions[f"{vt}.{Direction.LONG.value}"]
    assert pos.price == pytest.approx(11.0 * 1.02)  # 累乘只一次


def test_partial_sell_releases_position_freeze_proportionally() -> None:
    """卖单部分成交时，仅按成交量扣减 frozen，剩余仍冻结直至撤单或全成交。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0), quote=_quote(vt))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    # 先用全成交建仓 200 股
    _buy_then_fill(counter, vt, volume=200, price=11.0)
    counter.settle_end_of_day(date(2026, 4, 21))  # yd_volume → 200

    # 之后再切到 partial_rate=1.0，让卖单部分成交
    counter.partial_rate = 1.0

    _sell(counter, vt, volume=200, price=11.0)
    pos = counter.positions[f"{vt}.{Direction.LONG.value}"]
    # 部分成交后：剩余 frozen = 200 - 100 = 100
    assert pos.frozen == pytest.approx(100.0)
    # yd_volume 也按成交量 100 扣减
    assert pos.yd_volume == pytest.approx(100.0)


def test_buy_must_be_round_lot_but_sell_allows_odd_lot() -> None:
    """买入必须 100 股整数倍；卖出允许零股（A 股一次性卖尽零股规则）。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    sym, ex = vt.split(".")
    bad_buy = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=11.0, volume=150, reference="",
    )
    oid = _orderid(counter.send_order(bad_buy))
    assert counter.orders[oid].status == Status.REJECTED

    # 先建仓 200 股 + settle，让 yd_volume = 200
    _buy_then_fill(counter, vt, volume=200, price=11.0)
    counter.settle_end_of_day(date(2026, 4, 21))

    # 卖 150 股（非 100 倍数）应通过 send_order 校验
    odd_sell = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.SHORT, type=OrderType.LIMIT,
        offset=Offset.CLOSE, price=11.0, volume=150, reference="",
    )
    sid = _orderid(counter.send_order(odd_sell))
    assert counter.orders[sid].status != Status.REJECTED


def test_settle_on_zero_volume_position_does_not_apply_pct_chg() -> None:
    """空仓的 PositionData 不应被 mark-to-market 影响。"""
    vt = "000001.SZSE"
    md = _DummyMd(tick=_tick(vt, 11.0), quote=_quote(vt, pct_chg=5.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]

    sym, ex = vt.split(".")
    pos_key = f"{vt}.{Direction.LONG.value}"
    counter.positions[pos_key] = PositionData(
        symbol=sym, exchange=Exchange(ex), direction=Direction.LONG,
        volume=0, price=11.0, gateway_name="QMT_SIM",
    )
    counter.settle_end_of_day(date(2026, 4, 22))
    assert counter.positions[pos_key].price == pytest.approx(11.0)
