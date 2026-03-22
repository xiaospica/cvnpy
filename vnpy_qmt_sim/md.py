from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import SubscribeRequest
from .utils import to_qmt_code, parse_symbol_exchange

class QmtSimMd:
    """
    QMT模拟行情接口
    """

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name

    def connect(self):
        self.gateway.write_log("模拟行情接口连接成功")

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
        # 这里可以添加模拟行情推送逻辑，或者留空
