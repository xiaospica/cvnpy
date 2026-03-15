from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import SubscribeRequest

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
        self.gateway.write_log(f"模拟订阅: {req.symbol}")
        # 这里可以添加模拟行情推送逻辑，或者留空
