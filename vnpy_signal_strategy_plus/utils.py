from vnpy.trader.object import TickData
from vnpy.trader.constant import Direction
from vnpy.trader.utility import round_to


def choose_order_price(
    tick: TickData | None,
    direction: Direction,
    fallback_price: float,
    pricetick: float | None = None,
) -> float:
    price = 0.0

    if tick:
        if direction == Direction.LONG:
            price = float(tick.ask_price_1 or 0)
        else:
            price = float(tick.bid_price_1 or 0)

        if price <= 0:
            price = float(tick.last_price or 0)

        if tick.limit_up is not None and tick.limit_up and price > float(tick.limit_up):
            price = float(tick.limit_up)
        if tick.limit_down is not None and tick.limit_down and price < float(tick.limit_down):
            price = float(tick.limit_down)

        if pricetick:
            price = round_to(price, pricetick)

    if price <= 0:
        price = float(fallback_price or 0)

    return float(price)

# 把股票code转换成vnpy Exchange支持类型
def convert_code_to_vnpy_type(code: str) -> str:
    code = code.split(".")[0]
    if code.startswith(('5','6')):
        return code + '.SSE'
    else:
        return code + '.SZSE'
