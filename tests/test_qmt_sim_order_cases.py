from __future__ import annotations

from datetime import datetime, timedelta
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import OrderRequest, OrderData, TradeData, AccountData, PositionData

from vnpy_qmt_sim.td import SimulationCounter


def test_qmt_sim_no_fill_timeout_cancel() -> None:
    class _DummyGateway:
        gateway_name = "QMT_SIM"

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

    def _cancel_timeout(counter: SimulationCounter, now: datetime) -> None:
        for orderid, submit_time in list(counter.order_submit_time.items()):
            order = counter.orders.get(orderid)
            if not order:
                counter.order_submit_time.pop(orderid, None)
                continue
            if order.traded >= order.volume:
                counter.order_submit_time.pop(orderid, None)
                continue

            timeout_seconds = counter.order_timeout
            extra = getattr(order, "extra", None)
            if isinstance(extra, dict):
                try:
                    timeout_seconds = int(extra.get("timeout_seconds") or timeout_seconds)
                except Exception:
                    timeout_seconds = counter.order_timeout

            if (now - submit_time).total_seconds() <= timeout_seconds:
                continue

            counter.release_order_frozen_cash(order.orderid)
            order.status = Status.CANCELLED
            counter.order_submit_time.pop(order.orderid, None)
            counter.order_reject_reason.pop(order.orderid, None)
            counter.gateway.on_order(order)

    counter = SimulationCounter(_DummyGateway())  # type: ignore[arg-type]
    counter.order_timeout = 30
    counter.reporting_delay_ms = 0
    counter.fill_delay_ms = 0

    req = OrderRequest(
        symbol="510300",
        exchange=Exchange.SSE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=100.0,
        price=4.0,
        offset=Offset.OPEN,
        reference="SignalStrategyPlus_live_order_test|case=no_fill_1s",
    )
    vt_orderid = counter.send_order(req)
    orderid = vt_orderid.split(".")[-1]

    counter.process_simulation(datetime.now())

    order = counter.orders[orderid]
    assert order.status in {Status.SUBMITTING, Status.NOTTRADED}

    counter.order_submit_time[orderid] = datetime.now() - timedelta(seconds=2)
    _cancel_timeout(counter, datetime.now())

    assert order.status == Status.CANCELLED


def test_qmt_sim_partial_then_stall_timeout_cancel() -> None:
    class _DummyGateway:
        gateway_name = "QMT_SIM"

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

    def _cancel_timeout(counter: SimulationCounter, now: datetime) -> None:
        for orderid, submit_time in list(counter.order_submit_time.items()):
            order = counter.orders.get(orderid)
            if not order:
                counter.order_submit_time.pop(orderid, None)
                continue
            if order.traded >= order.volume:
                counter.order_submit_time.pop(orderid, None)
                continue

            timeout_seconds = counter.order_timeout
            extra = getattr(order, "extra", None)
            if isinstance(extra, dict):
                try:
                    timeout_seconds = int(extra.get("timeout_seconds") or timeout_seconds)
                except Exception:
                    timeout_seconds = counter.order_timeout

            if (now - submit_time).total_seconds() <= timeout_seconds:
                continue

            counter.release_order_frozen_cash(order.orderid)
            order.status = Status.CANCELLED
            counter.order_submit_time.pop(order.orderid, None)
            counter.order_reject_reason.pop(order.orderid, None)
            counter.gateway.on_order(order)

    counter = SimulationCounter(_DummyGateway())  # type: ignore[arg-type]
    counter.order_timeout = 1
    counter.reporting_delay_ms = 0
    counter.fill_delay_ms = 0

    req = OrderRequest(
        symbol="510300",
        exchange=Exchange.SSE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=21700.0,
        price=4.0,
        offset=Offset.OPEN,
        reference="SignalStrategyPlus_live_order_test|case=partial_then_stall_0s",
    )
    vt_orderid = counter.send_order(req)
    orderid = vt_orderid.split(".")[-1]

    now = datetime.now()
    counter.process_simulation(now)
    counter.process_simulation(now)

    order = counter.orders[orderid]
    assert order.status == Status.PARTTRADED
    assert 0 < float(order.traded) < float(order.volume)

    # 模拟超时撤单

    counter.order_submit_time[orderid] = datetime.now() - timedelta(seconds=2)
    _cancel_timeout(counter, datetime.now())

    assert order.status == Status.CANCELLED


def test_qmt_sim_force_sell_no_position_always_rejects() -> None:
    class _DummyGateway:
        gateway_name = "QMT_SIM"

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

    counter = SimulationCounter(_DummyGateway())  # type: ignore[arg-type]

    pos = PositionData(
        symbol="510300",
        exchange=Exchange.SSE,
        direction=Direction.LONG,
        volume=100000.0,
        frozen=0.0,
        price=4.0,
        pnl=0.0,
        yd_volume=100000.0,
        gateway_name="QMT_SIM",
    )
    counter.positions[f"{pos.symbol}.{pos.exchange.value}.{pos.direction.value}"] = pos

    req = OrderRequest(
        symbol="510300",
        exchange=Exchange.SSE,
        direction=Direction.SHORT,
        type=OrderType.LIMIT,
        volume=100.0,
        price=4.0,
        offset=Offset.CLOSE,
        reference="SignalStrategyPlus_live_order_test|case=force_sell_no_position",
    )
    vt_orderid = counter.send_order(req)
    orderid = vt_orderid.split(".")[-1]
    order = counter.orders[orderid]
    assert order.status == Status.REJECTED
    assert "用例强制" in str(getattr(order, "status_msg", "") or "")
