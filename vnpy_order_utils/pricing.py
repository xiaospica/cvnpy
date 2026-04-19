"""盘口定价工具 — 订单重挂与初次挂单共用.

choose_order_price: 根据 Tick 五档 + 涨跌停约束计算目标挂单价.
convert_code_to_vnpy_type: A 股代码前缀识别交易所并拼 vnpy vt_symbol.

原位于 vnpy_signal_strategy_plus/utils.py, 因为 auto_resubmit 也依赖 choose_order_price,
为避免 vnpy_order_utils 反向 import vnpy_signal_strategy_plus, 把 pricing 工具
一并迁过来. signal_strategy_plus/utils.py 保留 re-export shim.
"""

from vnpy.trader.object import TickData
from vnpy.trader.constant import Direction
from vnpy.trader.utility import round_to


def choose_order_price(
    tick: TickData | None,
    direction: Direction,
    fallback_price: float,
    pricetick: float | None = None,
) -> float:
    """按买卖方向取对手一档, 回退到 last_price, 涨跌停夹紧, 可选按最小价位取整."""
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


def convert_code_to_vnpy_type(code: str) -> str:
    """把股票 code 转成 vnpy vt_symbol 格式. 600000/601xxx/5xxxxx → .SSE, 否则 .SZSE."""
    code = code.split(".")[0]
    if code.startswith(('5', '6')):
        return code + '.SSE'
    else:
        return code + '.SZSE'
