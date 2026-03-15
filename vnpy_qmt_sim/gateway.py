# -*- coding: utf-8 -*-
from typing import Dict, List, Any
from vnpy.event import EventEngine
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    SubscribeRequest,
    OrderRequest,
    CancelRequest,
    AccountData,
    PositionData
)
from vnpy.trader.constant import Exchange

from .md import QmtSimMd
from .td import QmtSimTd


class QmtSimGateway(BaseGateway):
    """
    QMT模拟网关
    """

    default_name = "QMT_SIM"

    default_setting: Dict[str, Any] = {
        "模拟资金": 10000000.0,
        "部分成交率": 0.0,
        "拒单率": 0.0
    }

    exchanges = [Exchange.SSE, Exchange.SZSE]

    def __init__(self, event_engine: EventEngine, gateway_name: str):
        super().__init__(event_engine, gateway_name)
        
        self.md = QmtSimMd(self)
        self.td = QmtSimTd(self)

    def connect(self, setting: dict):
        self.md.connect()
        self.td.connect(setting)
        
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
        pass
