from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from vnpy.trader.constant import Exchange
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import SubscribeRequest, TickData
from vnpy.trader.utility import extract_vt_symbol, round_to

from .bar_source import BarQuote, SimBarSource
from .utils import to_qmt_code, parse_symbol_exchange


class QmtSimMd:
    """QMT 模拟行情接口。通过注入 SimBarSource 取真实参考价；source 不可用时退化为合成 tick。"""

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.source: Optional[SimBarSource] = None
        self._tick_cache: dict[str, TickData] = {}
        self._quote_cache: dict[str, BarQuote] = {}

    def connect(self):
        if self.source is not None:
            self.gateway.write_log(f"模拟行情接口连接成功 (source={self.source.name})")
        else:
            self.gateway.write_log("模拟行情接口连接成功 (无 source，使用合成 tick)")

    def get_full_tick(self, vt_symbol: str) -> Optional[TickData]:
        return self._tick_cache.get(vt_symbol)

    def get_quote(self, vt_symbol: str) -> Optional[BarQuote]:
        return self._quote_cache.get(vt_symbol)

    def refresh_tick(self, vt_symbol: str, as_of_date: Optional[date] = None) -> Optional[TickData]:
        """按 as_of_date 从 source 取最新参考价生成 tick 并缓存。source 缺失时退化为合成 tick。"""
        if self.source is None:
            return self._tick_cache.get(vt_symbol) or self.set_synthetic_tick(vt_symbol, last_price=10.0, pricetick=0.01)

        when = as_of_date or datetime.now().date()
        quote = self.source.get_quote(vt_symbol, when)
        if quote is None:
            self.gateway.write_log(
                f"bar_source 未命中 {vt_symbol}@{when}，退化合成 tick (last=10.0)"
            )
            return self.set_synthetic_tick(vt_symbol, last_price=10.0, pricetick=0.01)

        symbol, exchange = extract_vt_symbol(vt_symbol)
        tick = TickData(
            symbol=symbol,
            exchange=exchange,
            datetime=datetime.now(),
            name=quote.name,
            volume=0,
            turnover=0,
            open_interest=0,
            last_price=quote.last_price,
            last_volume=0,
            limit_up=quote.limit_up,
            limit_down=quote.limit_down,
            open_price=quote.open_price,
            high_price=quote.high_price,
            low_price=quote.low_price,
            pre_close=quote.pre_close,
            bid_price_1=max(quote.last_price - quote.pricetick, quote.limit_down),
            bid_volume_1=0,
            ask_price_1=min(quote.last_price + quote.pricetick, quote.limit_up),
            ask_volume_1=0,
            gateway_name=self.gateway_name,
        )
        self._tick_cache[vt_symbol] = tick
        self._quote_cache[vt_symbol] = quote
        return tick

    def set_synthetic_tick(
        self,
        vt_symbol: str,
        last_price: float,
        pricetick: float | None = None,
        limit_ratio: float = 0.1,
    ) -> TickData:
        """合成 tick（无数据源时的保底实现，保留原语义）。"""
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

        if self.source is not None:
            self.refresh_tick(req.vt_symbol)
            return

        if req.vt_symbol not in self._tick_cache:
            try:
                self.set_synthetic_tick(req.vt_symbol, last_price=10.0, pricetick=0.01)
            except Exception:
                pass
