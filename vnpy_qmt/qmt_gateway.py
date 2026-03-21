# -*- coding:utf-8 -*-
"""
@FileName  :qmt_gateway.py
@Time      :2022/11/8 16:49
@Author    :fsksf
"""
from typing import Dict, Any
from datetime import datetime
from vnpy.event import Event, EventEngine
from vnpy.trader.event import (
    EVENT_TIMER,
    EVENT_TICK
)
from vnpy.trader.constant import (
    Product, Direction, OrderType, Exchange, Status

)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    ContractData,
)

from vnpy_qmt.md import MD
from vnpy_qmt.td import TD


class QmtGateway(BaseGateway):

    default_name = "QMT"

    default_setting: Dict[str, Any] = {
        "交易账号": "",
        "mini路径": "",
        "是否启用超时撤单": False,
        "订单超时秒数": 30,
        "超时撤单检查周期秒": 1
    }

    TRADE_TYPE = (Product.ETF, Product.EQUITY, Product.BOND, Product.INDEX)
    exchanges = (Exchange.SSE, Exchange.SZSE)

    def __init__(self, event_engine: EventEngine, gateway_name: str = 'QMT'):
        super(QmtGateway, self).__init__(event_engine, gateway_name)
        self.contracts: Dict[str, ContractData] = {}
        self.md = MD(self)
        self.td = TD(self)
        self.count = -1
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        self.timeout_cancel_enabled: bool = False
        self.order_timeout_seconds: int = 30
        self.timeout_check_interval_seconds: int = 1
        self._last_timeout_check: datetime = datetime.now()
        self._cancel_sent: set[str] = set()
        self.connected = False

    def connect(self, setting: dict) -> None:
        self.md.connect(setting)
        self.td.connect(setting)
        self.timeout_cancel_enabled = bool(setting.get("是否启用超时撤单", False))
        self.order_timeout_seconds = int(setting.get("订单超时秒数", 30))
        self.timeout_check_interval_seconds = int(setting.get("超时撤单检查周期秒", 1))
        self.write_log("QMT网关连接成功")
        self.connected = True

    def close(self) -> None:
        self.md.close()

    def subscribe(self, req: SubscribeRequest) -> None:
        return self.md.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        return self.td.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        return self.td.cancel_order(req.orderid)

    def query_account(self) -> None:
        self.td.query_account()

    def query_position(self) -> None:
        self.td.query_position()

    def query_order(self):
        self.td.query_order()

    def query_trade(self):
        self.td.query_trade()

    def on_contract(self, contract):
        self.contracts[contract.vt_symbol] = contract
        super(QmtGateway, self).on_contract(contract)

    def get_contract(self, vt_symbol):
        return self.contracts.get(vt_symbol)

    def process_timer_event(self, event) -> None:
        """定时驱动订单查询与超时撤单检查。"""
        if not self.td.inited:
            return
        if self.count == -1:
            self.query_trade()
        self.count += 1

        if self.count % 5 == 0:
            self.query_order()

        if self.count % 7 == 0:
            self.query_account()
            self.query_position()
        if self.count < 21:
            self.check_order_timeout()
            return
        self.count = 0
        self.check_order_timeout()

    def check_order_timeout(self) -> None:
        """扫描超时活动订单并发起撤单请求（实盘行为，默认关闭）。"""
        if not self.timeout_cancel_enabled:
            return

        if self.order_timeout_seconds <= 0:
            return

        if self.timeout_check_interval_seconds <= 0:
            self.timeout_check_interval_seconds = 1

        now = datetime.now()
        if (now - self._last_timeout_check).total_seconds() < self.timeout_check_interval_seconds:
            return
        self._last_timeout_check = now

        for orderid, order in list(self.td.orders.items()):
            if order.status in [Status.ALLTRADED, Status.CANCELLED, Status.REJECTED]:
                self._cancel_sent.discard(orderid)
                continue

            if orderid in self._cancel_sent:
                continue

            if order.traded >= order.volume:
                continue

            if order.status not in [Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED]:
                continue

            submit_time = self.td.order_submit_time.get(orderid)
            if not submit_time:
                continue

            if (now - submit_time).total_seconds() < self.order_timeout_seconds:
                continue

            if not getattr(order, "reference", ""):
                continue

            self.write_log(f"订单超时自动撤单: {orderid}")
            self.td.cancel_order(orderid)
            self._cancel_sent.add(orderid)

    def write_log(self, msg):
        super(QmtGateway, self).write_log(f"[QMT] {msg}")


if __name__ == '__main__':
    qmt = QmtGateway(None)
    qmt.subscribe(SubscribeRequest(symbol='000001', exchange=Exchange.SZSE))
    qmt.md.get_contract()

    import threading
    import time

    def slp():
        while True:
            time.sleep(0.1)
    t = threading.Thread(target=slp)
    t.start()
    t.join()
