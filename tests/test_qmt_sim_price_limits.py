from __future__ import annotations

from datetime import datetime

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import AccountData, OrderData, OrderRequest, PositionData, TickData, TradeData

from vnpy_qmt_sim.td import SimulationCounter


def test_qmt_sim_price_limit_reject_up() -> None:
    class _DummyMd:
        def __init__(self, tick: TickData):
            self._tick = tick

        def get_full_tick(self, vt_symbol: str) -> TickData | None:
            if vt_symbol == self._tick.vt_symbol:
                return self._tick
            return None

    class _DummyGateway:
        gateway_name = "QMT_SIM"

        def __init__(self, md: _DummyMd):
            self.md = md

        def on_order(self, order: OrderData) -> None:
            return

        def on_trade(self, trade: TradeData) -> None:
            return

        def on_account(self, account: AccountData) -> None:
            return

        def on_position(self, position: PositionData) -> None:
            return

        def write_log(self, msg: str) -> None:
            return

    tick = TickData(
        gateway_name="QMT_SIM",
        symbol="510300",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        last_price=4.0,
        limit_up=4.4,
        limit_down=3.6,
        bid_price_1=3.99,
        ask_price_1=4.01,
    )

    counter = SimulationCounter(_DummyGateway(_DummyMd(tick)))  # type: ignore[arg-type]

    req = OrderRequest(
        symbol="510300",
        exchange=Exchange.SSE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=100.0,
        price=4.41,
        offset=Offset.OPEN,
        reference="SignalStrategyPlus_live_order_test|case=reject_up",
    )
    vt_orderid = counter.send_order(req)
    orderid = vt_orderid.split(".")[-1]
    order = counter.orders[orderid]
    assert order.status == Status.REJECTED
    assert "涨停" in str(getattr(order, "status_msg", "") or "")

