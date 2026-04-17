from datetime import datetime

from vnpy.trader.constant import Direction, Exchange
from vnpy.trader.object import TickData

from vnpy_signal_strategy_plus.utils import choose_order_price


def test_choose_price_buy_ask1() -> None:
    tick = TickData(
        gateway_name="TEST",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        ask_price_1=10.2,
        bid_price_1=10.1,
        last_price=10.15,
    )

    price = choose_order_price(tick, Direction.LONG, fallback_price=9.9, pricetick=0.01)
    assert price == 10.2


def test_choose_price_sell_bid1() -> None:
    tick = TickData(
        gateway_name="TEST",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        ask_price_1=10.2,
        bid_price_1=10.1,
        last_price=10.15,
    )

    price = choose_order_price(tick, Direction.SHORT, fallback_price=9.9, pricetick=0.01)
    assert price == 10.1


def test_choose_price_fallback_last_price() -> None:
    tick = TickData(
        gateway_name="TEST",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        ask_price_1=0,
        bid_price_1=0,
        last_price=10.15,
    )

    price = choose_order_price(tick, Direction.LONG, fallback_price=9.9, pricetick=0.01)
    assert price == 10.15


def test_choose_price_fallback_signal_price() -> None:
    price = choose_order_price(None, Direction.LONG, fallback_price=9.9, pricetick=0.01)
    assert price == 9.9


def test_choose_price_clamp_limit_up() -> None:
    tick = TickData(
        gateway_name="TEST",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        ask_price_1=10.2,
        last_price=10.15,
        limit_up=10.0,
        limit_down=9.0,
    )

    price = choose_order_price(tick, Direction.LONG, fallback_price=9.9, pricetick=0.01)
    assert price == 10.0


def test_choose_price_round_to_pricetick() -> None:
    tick = TickData(
        gateway_name="TEST",
        symbol="600000",
        exchange=Exchange.SSE,
        datetime=datetime.now(),
        ask_price_1=10.023,
        last_price=10.0,
    )

    price = choose_order_price(tick, Direction.LONG, fallback_price=9.9, pricetick=0.01)
    assert price == 10.02

