from datetime import datetime, timedelta

import pytest

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Product, Direction, Offset, Status, OrderType
from vnpy.trader.object import ContractData, OrderData

import vnpy_xt.xt_gateway as xt_gateway


def _setup_contract(vt_symbol: str) -> ContractData:
    symbol, exchange_str = vt_symbol.split(".")
    contract = ContractData(
        symbol=symbol,
        exchange=Exchange(exchange_str),
        name="TEST",
        product=Product.EQUITY,
        size=100,
        pricetick=0.01,
        gateway_name="XT",
    )
    xt_gateway.symbol_contract_map[vt_symbol] = contract
    xt_gateway.symbol_limit_map[vt_symbol] = (11.0, 9.0)
    return contract


def test_xt_get_full_tick_builds_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    vt_symbol = "600000.SSE"
    _setup_contract(vt_symbol)

    engine = EventEngine()
    gw = xt_gateway.XtGateway(engine, "XT")

    def fake_get_full_tick(arg):
        xt_symbol = "600000.SH"
        payload = {
            "time": int(datetime.now().timestamp() * 1000),
            "volume": 1,
            "amount": 10,
            "openInt": 0,
            "bidPrice": [9.99, 0, 0, 0, 0],
            "askPrice": [10.01, 0, 0, 0, 0],
            "bidVol": [1, 0, 0, 0, 0],
            "askVol": [1, 0, 0, 0, 0],
            "lastPrice": 10.0,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "lastClose": 10.0,
            "settlementPrice": 0,
        }

        if isinstance(arg, list):
            return {xt_symbol: [payload]}
        return {xt_symbol: [payload]}

    monkeypatch.setattr(xt_gateway.xtdata, "get_full_tick", fake_get_full_tick, raising=False)

    tick = gw.get_full_tick(vt_symbol)
    assert tick is not None
    assert tick.vt_symbol == vt_symbol
    assert tick.ask_price_1 == 10.01
    assert tick.bid_price_1 == 9.99
    assert tick.name == "TEST"
    assert tick.limit_up == 11.0
    assert tick.limit_down == 9.0


def test_xt_check_order_timeout_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = EventEngine()
    gw = xt_gateway.XtGateway(engine, "XT")

    gw.trading = True
    gw.timeout_cancel_enabled = True
    gw.order_timeout_seconds = 1
    gw.timeout_check_interval_seconds = 1
    gw._last_timeout_check = datetime.now() - timedelta(seconds=10)

    orderid = "OID1"
    order = OrderData(
        symbol="600000",
        exchange=Exchange.SSE,
        orderid=orderid,
        direction=Direction.LONG,
        offset=Offset.NONE,
        type=OrderType.LIMIT,
        price=10.0,
        volume=100,
        traded=0,
        status=Status.NOTTRADED,
        datetime=datetime.now(),
        gateway_name="XT",
    )
    gw.orders[orderid] = order
    gw.order_submit_time[orderid] = datetime.now() - timedelta(seconds=100)
    gw.td_api.active_localid_sysid_map[orderid] = "SYSID"

    called = {"count": 0}

    def fake_cancel(req):
        called["count"] += 1

    monkeypatch.setattr(gw.td_api, "cancel_order", fake_cancel)

    gw.check_order_timeout()
    assert called["count"] == 1

    gw._last_timeout_check = datetime.now() - timedelta(seconds=10)
    gw.check_order_timeout()
    assert called["count"] == 1

