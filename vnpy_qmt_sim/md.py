from datetime import datetime

from vnpy.trader.constant import Exchange
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import SubscribeRequest, TickData
from vnpy.trader.utility import extract_vt_symbol, round_to

from .utils import to_qmt_code, parse_symbol_exchange


class QmtSimMd:
    """
    QMT模拟行情接口
    """

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self._tick_cache: dict[str, TickData] = {}

    def connect(self):
        self.gateway.write_log("模拟行情接口连接成功")

    def get_full_tick(self, vt_symbol: str) -> TickData | None:
        return self._tick_cache.get(vt_symbol)

    def set_synthetic_tick(
        self,
        vt_symbol: str,
        last_price: float,
        pricetick: float | None = None,
        limit_ratio: float = 0.1,
    ) -> TickData:
        symbol, exchange = extract_vt_symbol(vt_symbol)

        last_price = float(last_price or 0)
        if last_price <= 0:
            last_price = 1.0

        if pricetick and pricetick > 0:
            last_price = round_to(last_price, float(pricetick))

        limit_up = last_price * (1 + float(limit_ratio))
        limit_down = last_price * (1 - float(limit_ratio))
        if pricetick and pricetick > 0:
            limit_up = round_to(limit_up, float(pricetick))
            limit_down = round_to(limit_down, float(pricetick))

        bid1 = max(last_price - (float(pricetick) if pricetick else 0.001), limit_down)
        ask1 = min(last_price + (float(pricetick) if pricetick else 0.001), limit_up)
        if pricetick and pricetick > 0:
            bid1 = round_to(bid1, float(pricetick))
            ask1 = round_to(ask1, float(pricetick))

        tick = TickData(
            symbol=symbol,
            exchange=exchange,
            datetime=datetime.now(),
            name="",
            volume=0,
            turnover=0,
            open_interest=0,
            last_price=last_price,
            last_volume=0,
            limit_up=limit_up,
            limit_down=limit_down,
            open_price=last_price,
            high_price=last_price,
            low_price=last_price,
            pre_close=last_price,
            bid_price_1=bid1,
            bid_volume_1=0,
            ask_price_1=ask1,
            ask_volume_1=0,
            gateway_name=self.gateway_name,
        )

        self._tick_cache[vt_symbol] = tick
        return tick

    def subscribe(self, req: SubscribeRequest):
        symbol = req.symbol
        exchange = req.exchange

        if "." in symbol:
            parsed = parse_symbol_exchange(symbol)
            if parsed:
                symbol, exchange = parsed

        qmt_code = ""
        try:
            qmt_code = to_qmt_code(symbol, exchange)
        except Exception:
            qmt_code = symbol

        self.gateway.write_log(f"模拟订阅: {req.vt_symbol} -> {qmt_code}")
        if req.vt_symbol not in self._tick_cache:
            try:
                self.set_synthetic_tick(req.vt_symbol, last_price=10.0, pricetick=0.01)
            except Exception:
                pass
