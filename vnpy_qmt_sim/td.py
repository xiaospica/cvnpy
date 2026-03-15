from typing import Dict, List, Any
from datetime import datetime
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    OrderRequest,
    CancelRequest,
    OrderData,
    TradeData,
    PositionData,
    AccountData,
    LogData
)
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Status,
    Offset
)

class SimulationCounter:
    """模拟柜台"""

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.orders: Dict[str, OrderData] = {}
        self.trades: Dict[str, TradeData] = {}
        self.positions: Dict[str, PositionData] = {}
        self.accounts: Dict[str, AccountData] = {}
        
        self.order_count = 0
        self.trade_count = 0
        
        # 资金配置
        self.capital = 10_000_000.0
        self.frozen = 0.0
        
        # 异常配置
        self.reject_rate = 0.0  # 拒单率
        self.partial_rate = 0.0 # 部分成交率
        self.latency = 0 # 模拟延迟(ms)

    def send_order(self, req: OrderRequest) -> str:
        self.order_count += 1
        orderid = str(self.order_count)
        
        order = OrderData(
            symbol=req.symbol,
            exchange=req.exchange,
            orderid=orderid,
            type=req.type,
            direction=req.direction,
            offset=req.offset,
            price=req.price,
            volume=req.volume,
            traded=0,
            status=Status.SUBMITTING,
            datetime=datetime.now(),
            gateway_name=self.gateway.gateway_name
        )
        self.orders[orderid] = order
        self.gateway.on_order(order)
        
        # 模拟撮合
        self.match_order(order)
        
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest):
        order = self.orders.get(req.orderid)
        if not order:
            return
            
        if order.status in [Status.ALLTRADED, Status.CANCELLED, Status.REJECTED]:
            return
            
        order.status = Status.CANCELLED
        self.gateway.on_order(order)

    def match_order(self, order: OrderData):
        """模拟撮合逻辑"""
        # 简单的立即成交逻辑
        if order.status == Status.SUBMITTING:
            order.status = Status.NOTTRADED
            self.gateway.on_order(order)

        # 拒单模拟
        if self.reject_rate > 0:
            import random
            if random.random() < self.reject_rate:
                order.status = Status.REJECTED
                self.gateway.on_order(order)
                return

        # 全额成交
        trade_volume = order.volume - order.traded
        if trade_volume <= 0:
            return

        # 部分成交模拟
        if self.partial_rate > 0:
            import random
            if random.random() < self.partial_rate:
                trade_volume = trade_volume // 2
                if trade_volume == 0:
                    trade_volume = 1

        self.trade_count += 1
        trade = TradeData(
            symbol=order.symbol,
            exchange=order.exchange,
            orderid=order.orderid,
            tradeid=str(self.trade_count),
            direction=order.direction,
            offset=order.offset,
            price=order.price if order.price > 0 else 10.0, # 市价单简单模拟价格
            volume=trade_volume,
            datetime=datetime.now(),
            gateway_name=self.gateway.gateway_name
        )
        self.trades[trade.tradeid] = trade
        
        order.traded += trade_volume
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
        else:
            order.status = Status.PARTTRADED
            
        self.gateway.on_order(order)
        self.gateway.on_trade(trade)
        
        self.update_position(trade)
        self.update_account(trade)

    def update_position(self, trade: TradeData):
        vt_symbol = f"{trade.symbol}.{trade.exchange.value}"
        
        # A股通常只看多头持仓
        pos_long_id = f"{vt_symbol}.{Direction.LONG.value}"
        pos = self.positions.get(pos_long_id)
        
        if not pos:
            pos = PositionData(
                symbol=trade.symbol,
                exchange=trade.exchange,
                direction=Direction.LONG,
                volume=0,
                gateway_name=self.gateway.gateway_name
            )
            self.positions[pos_long_id] = pos

        if trade.direction == Direction.LONG:
            pos.volume += trade.volume
        else:
            pos.volume -= trade.volume
            
        # Ensure volume not negative
        if pos.volume < 0:
            pos.volume = 0
            
        self.gateway.on_position(pos)

    def update_account(self, trade: TradeData):
        # 简单扣款
        cost = trade.price * trade.volume
        if trade.direction == Direction.LONG:
            self.capital -= cost
        else:
            self.capital += cost
            
        account = AccountData(
            accountid="SIM_ACC",
            balance=self.capital,
            frozen=self.frozen,
            gateway_name=self.gateway.gateway_name
        )
        self.accounts[account.accountid] = account
        self.gateway.on_account(account)


class QmtSimTd:
    """
    QMT模拟交易接口
    """

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.counter = SimulationCounter(gateway)

    def connect(self, setting: dict):
        self.counter.capital = setting.get("模拟资金", 10000000.0)
        self.counter.partial_rate = setting.get("部分成交率", 0.0)
        self.counter.reject_rate = setting.get("拒单率", 0.0)
        
        self.gateway.write_log("模拟交易接口连接成功")
        
        # 推送初始账户
        account = AccountData(
            accountid="SIM_ACC",
            balance=self.counter.capital,
            frozen=self.counter.frozen,
            gateway_name=self.gateway_name
        )
        self.counter.accounts[account.accountid] = account
        self.gateway.on_account(account)

    def send_order(self, req: OrderRequest) -> str:
        return self.counter.send_order(req)

    def cancel_order(self, req: CancelRequest):
        self.counter.cancel_order(req)

    def query_account(self):
        """查询账户"""
        for account in self.counter.accounts.values():
            self.gateway.on_account(account)

    def query_position(self):
        """查询持仓"""
        for position in self.counter.positions.values():
            self.gateway.on_position(position)
            
    def query_orders(self):
        """查询委托"""
        for order in self.counter.orders.values():
            self.gateway.on_order(order)
            
    def query_trades(self):
        """查询成交"""
        for trade in self.counter.trades.values():
            self.gateway.on_trade(trade)
