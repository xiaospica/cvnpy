# -*- coding: utf-8 -*-
from typing import Dict, Any
from datetime import datetime
from vnpy.event import EventEngine, Event, EVENT_TIMER
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    SubscribeRequest,
    OrderRequest,
    CancelRequest,
    AccountData,
    PositionData
)
from vnpy.trader.constant import Exchange, Status

from .md import QmtSimMd
from .td import QmtSimTd


class QmtSimGateway(BaseGateway):
    """
    QMT模拟网关
    """

    default_name = "QMT_SIM"

    default_setting: Dict[str, Any] = {
        "账户": "test_id",
        "模拟资金": 10000000.0,
        "部分成交率": 0.0,
        "拒单率": 0.0,
        "订单超时秒数": 30,
    }

    exchanges = [Exchange.SSE, Exchange.SZSE]

    def __init__(self, event_engine: EventEngine, gateway_name: str):
        super().__init__(event_engine, gateway_name)
        
        self.md = QmtSimMd(self)
        self.td = QmtSimTd(self)
        self._timer_count = 0
        self._order_timeout_interval = 1

    def connect(self, setting: dict):
        """连接行情与交易模块，并注册超时检查定时任务。"""
        self.md.connect()
        self.td.connect(setting)
        self.td.counter.order_timeout = int(setting.get("订单超时秒数", 30))

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)
        
        self.write_log("模拟网关连接成功")

    def subscribe(self, req: SubscribeRequest):
        self.md.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        return self.td.send_order(req)

    def cancel_order(self, req: CancelRequest):
        self.td.cancel_order(req)

    def query_account(self):
        """查询账户"""
        self.td.query_account()

    def query_position(self):
        """查询持仓"""
        self.td.query_position()
        
    def query_orders(self):
        """查询委托"""
        self.td.query_orders()
        
    def query_trades(self):
        """查询成交"""
        self.td.query_trades()

    def close(self):
        try:
            self.event_engine.unregister(EVENT_TIMER, self.process_timer_event)
        except Exception:
            pass

    def process_timer_event(self, event: Event) -> None:
        """事件循环入口，按周期触发超时订单扫描。"""
        self._timer_count += 1

        if self._timer_count % self._order_timeout_interval == 0:
            self.check_order_timeout()

    def check_order_timeout(self) -> None:
        """扫描超时活动订单并执行撤单与冻结释放。"""
        now = datetime.now()
        timeout_orders = []
        for orderid, submit_time in list(self.td.counter.order_submit_time.items()):
            if (now - submit_time).total_seconds() <= self.td.counter.order_timeout:
                continue
            order = self.td.counter.orders.get(orderid)
            if not order:
                self.td.counter.order_submit_time.pop(orderid, None)
                continue
            if order.traded >= order.volume:
                self.td.counter.order_submit_time.pop(orderid, None)
                continue
            if order.status in [Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED]:
                timeout_orders.append(order)

        for order in timeout_orders:
            self.td.counter.release_order_frozen_cash(order.orderid)
            order.status = Status.CANCELLED
            self.td.counter.order_submit_time.pop(order.orderid, None)
            self.td.counter.order_reject_reason.pop(order.orderid, None)
            self.on_order(order)
            self.write_log(f"订单超时自动撤单: {order.orderid}")
