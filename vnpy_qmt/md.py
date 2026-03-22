# -*- coding:utf-8 -*-
"""
@FileName  :md.py
@Time      :2022/11/8 17:14
@Author    :fsksf
"""

from vnpy.trader.object import (
    CancelRequest, OrderRequest, SubscribeRequest, TickData,
    ContractData
)
from vnpy.trader.constant import Exchange
from datetime import datetime
import xtquant.xtdata
import xtquant.xttrader
import xtquant.xttype
from vnpy_qmt.utils import (
    From_VN_Exchange_map, TO_VN_Exchange_map, to_vn_contract,
    TO_VN_Product, to_vn_product, timestamp_to_datetime,
    to_qmt_code
)
from vnpy.trader.utility import ZoneInfo

ZONE_INFO = ZoneInfo("Asia/Shanghai")


class MD:

    def __init__(self, gateway):
        self.gateway = gateway
        self.th = None
        self.limit_ups = {}
        self.limit_downs = {}

    def close(self) -> None:
        pass

    def subscribe(self, req: SubscribeRequest) -> None:

        return xtquant.xtdata.subscribe_quote(
            stock_code=f'{req.symbol}.{From_VN_Exchange_map[req.exchange]}',
            period='tick',
            callback=self.on_tick
        )

    def connect(self, setting: dict) -> None:
        self.get_contract()
        return

    def get_contract(self):
        self.write_log('开始获取标的信息')
        contract_ids = set()
        bk = ['上期所', '上证A股', '上证B股', '中金所', '创业板', '大商所',
              '沪市ETF', '沪市指数', '沪深A股',
              '沪深B股', '沪深ETF', '沪深指数', '深市ETF',
              '深市基金', '深市指数', '深证A股', '深证B股', '科创板', '科创板CDR',
              ]
        for sector in bk:
            print(sector)
            stock_list = xtquant.xtdata.get_stock_list_in_sector(sector_name=sector)
            for symbol in stock_list:
                if symbol in contract_ids:
                    continue
                contract_ids.add(symbol)
                info = xtquant.xtdata.get_instrument_detail(symbol)
                contract_type = xtquant.xtdata.get_instrument_type(symbol)
                if info is None or contract_type is None:
                    continue
                try:
                    exchange = TO_VN_Exchange_map[info['ExchangeID']]
                except KeyError:

                    print('本gateway不支持的标的', symbol)
                    continue
                if exchange not in self.gateway.exchanges:
                    continue
                product = to_vn_product(contract_type)
                if product not in self.gateway.TRADE_TYPE:
                    continue

                c = ContractData(
                    gateway_name=self.gateway.gateway_name,
                    symbol=info['InstrumentID'],
                    exchange=exchange,
                    name=info['InstrumentName'],
                    product=product,
                    pricetick=info['PriceTick'],
                    size=100,
                    min_volume=100
                )
                self.limit_ups[c.vt_symbol] = info['UpStopPrice']
                self.limit_downs[c.vt_symbol] = info['DownStopPrice']
                self.gateway.on_contract(c)
        self.write_log('获取标的信息完成')

    def on_tick(self, datas):
        for code, data_list in datas.items():
            for data in data_list:
                tick = self._convert_xt_tick(code, data)
                if not tick:
                    continue
                contract = self.gateway.get_contract(tick.vt_symbol)
                if contract:
                    tick.name = contract.name
                tick.limit_up = self.limit_ups.get(tick.vt_symbol, None)
                tick.limit_down = self.limit_downs.get(tick.vt_symbol, None)
                self.gateway.on_tick(tick)

    def write_log(self, msg):
        self.gateway.write_log(f"[ md ] {msg}")

    def _convert_xt_tick(self, code: str, data: dict) -> TickData | None:
        try:
            symbol, suffix = code.rsplit(".")
        except ValueError:
            return None

        exchange = TO_VN_Exchange_map.get(suffix)
        if not exchange:
            return None

        ask_price = data.get("askPrice") or []
        ask_vol = data.get("askVol") or []
        bid_price = data.get("bidPrice") or []
        bid_vol = data.get("bidVol") or []

        def _get(seq: list, i: int) -> float:
            try:
                return float(seq[i])
            except Exception:
                return 0.0

        dt_raw = data.get("time", 0)
        try:
            if dt_raw:
                dt = timestamp_to_datetime(dt_raw).replace(tzinfo=ZONE_INFO)
            else:
                dt = datetime.now(ZONE_INFO)
        except Exception:
            dt = datetime.now(ZONE_INFO)

        tick = TickData(
            gateway_name=self.gateway.gateway_name,
            symbol=symbol,
            exchange=exchange,
            datetime=dt,
            last_price=float(data.get("lastPrice", 0) or 0),
            volume=float(data.get("volume", 0) or 0),
            open_price=float(data.get("open", 0) or 0),
            high_price=float(data.get("high", 0) or 0),
            low_price=float(data.get("low", 0) or 0),
            pre_close=float(data.get("lastClose", 0) or 0),
            limit_down=0,
            limit_up=0,
            ask_price_1=_get(ask_price, 0),
            ask_price_2=_get(ask_price, 1),
            ask_price_3=_get(ask_price, 2),
            ask_price_4=_get(ask_price, 3),
            ask_price_5=_get(ask_price, 4),
            ask_volume_1=_get(ask_vol, 0),
            ask_volume_2=_get(ask_vol, 1),
            ask_volume_3=_get(ask_vol, 2),
            ask_volume_4=_get(ask_vol, 3),
            ask_volume_5=_get(ask_vol, 4),
            bid_price_1=_get(bid_price, 0),
            bid_price_2=_get(bid_price, 1),
            bid_price_3=_get(bid_price, 2),
            bid_price_4=_get(bid_price, 3),
            bid_price_5=_get(bid_price, 4),
            bid_volume_1=_get(bid_vol, 0),
            bid_volume_2=_get(bid_vol, 1),
            bid_volume_3=_get(bid_vol, 2),
            bid_volume_4=_get(bid_vol, 3),
            bid_volume_5=_get(bid_vol, 4),
        )
        return tick

    def get_full_tick(self, vt_symbol: str) -> TickData | None:
        contract = self.gateway.get_contract(vt_symbol)
        if contract:
            stock_code = to_qmt_code(contract.symbol, contract.exchange)
        else:
            try:
                symbol, exchange_str = vt_symbol.split(".")
                exchange = Exchange(exchange_str)
            except Exception:
                return None
            stock_code = to_qmt_code(symbol, exchange)

        func = getattr(xtquant.xtdata, "get_full_tick", None)
        if not func:
            self.write_log("xtquant.xtdata.get_full_tick 不存在，无法主动获取五档行情")
            return None

        try:
            try:
                raw = func([stock_code])
            except TypeError:
                raw = func(stock_code)
        except Exception as e:
            import traceback

            self.write_log(f"主动获取五档行情失败: {vt_symbol} {e}\n{traceback.format_exc()}")
            return None

        data = None
        if isinstance(raw, dict):
            data = raw.get(stock_code)
            if data is None and contract:
                data = raw.get(to_qmt_code(contract.symbol, contract.exchange))
        else:
            data = raw

        if isinstance(data, list):
            if not data:
                return None
            data = data[-1]

        if not isinstance(data, dict):
            return None

        tick = self._convert_xt_tick(stock_code, data)
        if not tick:
            tick = self._convert_xt_tick(stock_code, {"lastPrice": data.get("lastPrice", 0)})
            if not tick:
                return None

        if contract:
            tick.name = contract.name
        tick.limit_up = self.limit_ups.get(tick.vt_symbol, None)
        tick.limit_down = self.limit_downs.get(tick.vt_symbol, None)
        return tick
