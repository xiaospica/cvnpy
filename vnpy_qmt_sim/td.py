from typing import Dict, List, Any
from datetime import datetime, timedelta
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
        
        self.accountid = "test_id"
        # 资金配置
        self.capital = 10_000_000.0
        self.frozen = 0.0
        self.order_frozen_cash: Dict[str, float] = {}
        self.order_reject_reason: Dict[str, str] = {}

        self.commission_rate = 0.0001
        self.min_commission = 5.0
        self.transfer_fee_rate = 0.00001
        self.stamp_duty_rate = 0.0005

        # self.commission_rate = 0.0
        # self.min_commission = 0.0
        # self.transfer_fee_rate = 0.0
        # self.stamp_duty_rate = 0.0
        
        # 异常配置
        self.reject_rate = 0.0  # 拒单率
        self.partial_rate = 0.0 # 部分成交率
        self.latency = 0 # 模拟延迟(ms)
        
        # 超时配置
        self.order_timeout = 30  # 订单超时秒数
        self.order_submit_time: Dict[str, datetime] = {}

        self.fill_delay_ms: int = 0
        self.reporting_delay_ms: int = 0
        self.reject_short_if_no_position: bool = True
        self.order_tasks: Dict[str, Dict[str, Any]] = {}

    def process_simulation(self, now: datetime) -> None:
        for orderid, task in list(self.order_tasks.items()):
            order = self.orders.get(orderid)
            if not order:
                self.order_tasks.pop(orderid, None)
                continue

            if order.status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
                self.order_tasks.pop(orderid, None)
                continue

            phase = str(task.get("phase") or "")
            if phase == "unreported":
                report_at = task.get("report_at")
                if isinstance(report_at, datetime) and now < report_at:
                    continue
                order.status = Status.NOTTRADED
                self._set_order_status_msg(order, str(task.get("status_msg") or ""))
                self._set_order_extra(order, {"qmt_status": "ORDER_REPORTED", "case_tag": task.get("case_tag")})
                self.gateway.on_order(order)
                task["phase"] = "reported"
                continue

            if phase != "reported":
                continue

            case_tag = str(task.get("case_tag") or "")
            if case_tag.startswith("force_reject"):
                self._reject_order(order, str(task.get("status_msg") or "模拟强制拒单"))
                self.order_tasks.pop(orderid, None)
                continue

            if case_tag.startswith("no_fill"):
                continue

            if case_tag.startswith("partial_then_stall"):
                if not task.get("did_partial"):
                    partial_at = task.get("partial_at")
                    if isinstance(partial_at, datetime) and now < partial_at:
                        continue
                    ratio = float(task.get("partial_ratio") or 0.5)
                    remain = int(order.volume - order.traded)
                    if remain <= 0:
                        self.order_tasks.pop(orderid, None)
                        continue
                    trade_volume = max(int(remain * ratio), 1)
                    trade_volume = min(trade_volume, remain)
                    self._execute_trade(order, float(trade_volume))
                    task["did_partial"] = True
                continue

            fill_at = task.get("fill_at")
            if isinstance(fill_at, datetime) and now < fill_at:
                continue

            self.match_order(order)
            if order.status in {Status.ALLTRADED, Status.CANCELLED, Status.REJECTED}:
                self.order_tasks.pop(orderid, None)

    def _parse_case_tag(self, reference: str) -> str:
        if not reference:
            return ""
        marker = "|case="
        idx = reference.find(marker)
        if idx < 0:
            return ""
        tail = reference[idx + len(marker):]
        tag = tail.split("|", 1)[0].strip()
        return tag

    def _set_order_extra(self, order: OrderData, extra: Dict[str, Any]) -> None:
        try:
            old_extra = getattr(order, "extra", None)
            if isinstance(old_extra, dict):
                merged = {**old_extra, **extra}
                setattr(order, "extra", merged)
            else:
                setattr(order, "extra", dict(extra))
        except Exception:
            return

    def _set_order_status_msg(self, order: OrderData, msg: str) -> None:
        try:
            if msg:
                order.status_msg = msg
        except Exception:
            return

    def _reject_order(self, order: OrderData, status_msg: str) -> None:
        if order.direction == Direction.LONG:
            self.release_order_frozen_cash(order.orderid, push_event=False)
        order.status = Status.REJECTED
        self._set_order_status_msg(order, status_msg)
        self._set_order_extra(order, {"status_msg": status_msg, "qmt_status": "ORDER_JUNK"})
        self.order_submit_time.pop(order.orderid, None)
        self.order_reject_reason[order.orderid] = "case_reject"
        self.gateway.on_order(order)
        self.push_account()
        try:
            self.gateway.write_log(f"模拟拒单：{order.vt_orderid} {status_msg}")
        except Exception:
            return

    def _execute_trade(self, order: OrderData, volume: float) -> None:
        remain = float(order.volume - order.traded)
        if volume <= 0 or remain <= 0:
            return
        if volume > remain:
            volume = remain

        self.trade_count += 1
        trade = TradeData(
            symbol=order.symbol,
            exchange=order.exchange,
            orderid=order.orderid,
            tradeid=str(self.trade_count),
            direction=order.direction,
            offset=order.offset,
            price=order.price if order.price > 0 else 10.0,
            volume=volume,
            datetime=datetime.now(),
            gateway_name=self.gateway.gateway_name,
        )
        self.trades[trade.tradeid] = trade

        order.traded += volume
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
            self.order_submit_time.pop(order.orderid, None)
        else:
            order.status = Status.PARTTRADED

        self.gateway.on_order(order)
        self.gateway.on_trade(trade)
        self.update_position(trade)
        self.update_account(trade)
        try:
            extra = getattr(order, "extra", None)
            case_tag = ""
            if isinstance(extra, dict):
                case_tag = str(extra.get("case_tag") or "")
            if case_tag:
                self.gateway.write_log(f"模拟成交触发: {order.vt_orderid} case={case_tag} traded={order.traded}/{order.volume} status={order.status}")
        except Exception:
            return

    def send_order(self, req: OrderRequest) -> str:
        self.order_count += 1
        orderid = str(self.order_count)
        case_tag = self._parse_case_tag(str(getattr(req, "reference", "") or ""))
        
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
        self.order_submit_time[orderid] = order.datetime
        self._set_order_extra(order, {"qmt_status": "ORDER_UNREPORTED", "case_tag": case_tag})

        vol_int = 0
        try:
            vol_int = int(float(order.volume))
        except Exception:
            vol_int = 0
        if vol_int <= 0 or vol_int % 100 != 0:
            order.status = Status.REJECTED
            msg = f"委托数量不合法: volume={order.volume}"
            self._set_order_status_msg(order, msg)
            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK"})
            self.order_reject_reason[orderid] = "invalid_volume"
            self.order_submit_time.pop(orderid, None)
            self.gateway.on_order(order)
            self.gateway.write_log(f"拒单：{msg}")
            return order.vt_orderid

        if order.direction == Direction.SHORT and case_tag == "force_sell_no_position":
            order.status = Status.REJECTED
            self._set_order_status_msg(order, "持仓不足(用例强制)")
            self._set_order_extra(order, {"status_msg": "持仓不足(用例强制)", "qmt_status": "ORDER_JUNK"})
            self.order_reject_reason[orderid] = "force_sell_no_position"
            self.order_submit_time.pop(orderid, None)
            self.gateway.on_order(order)
            self.gateway.write_log("拒单：持仓不足(用例强制)")
            return order.vt_orderid

        if float(order.price) > 0:
            try:
                md = getattr(self.gateway, "md", None)
                get_tick = getattr(md, "get_full_tick", None)
                if callable(get_tick):
                    tick = get_tick(order.vt_symbol)
                    if tick:
                        limit_up = float(getattr(tick, "limit_up", 0) or 0)
                        limit_down = float(getattr(tick, "limit_down", 0) or 0)
                        if limit_up > 0 and float(order.price) > limit_up:
                            order.status = Status.REJECTED
                            msg = f"价格超出涨停: price={order.price} limit_up={limit_up}"
                            self._set_order_status_msg(order, msg)
                            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK", "limit_up": limit_up, "limit_down": limit_down})
                            self.order_reject_reason[orderid] = "price_limit_up"
                            self.order_submit_time.pop(orderid, None)
                            self.gateway.on_order(order)
                            self.gateway.write_log(msg)
                            return order.vt_orderid
                        if limit_down > 0 and float(order.price) < limit_down:
                            order.status = Status.REJECTED
                            msg = f"价格超出跌停: price={order.price} limit_down={limit_down}"
                            self._set_order_status_msg(order, msg)
                            self._set_order_extra(order, {"status_msg": msg, "qmt_status": "ORDER_JUNK", "limit_up": limit_up, "limit_down": limit_down})
                            self.order_reject_reason[orderid] = "price_limit_down"
                            self.order_submit_time.pop(orderid, None)
                            self.gateway.on_order(order)
                            self.gateway.write_log(msg)
                            return order.vt_orderid
            except Exception:
                pass

        if order.direction == Direction.SHORT and self.reject_short_if_no_position:
            pos_key = f"{order.symbol}.{order.exchange.value}.{Direction.LONG.value}"
            pos = self.positions.get(pos_key)
            pos_volume = float(pos.volume) if pos else 0.0
            if float(order.volume) > pos_volume:
                order.status = Status.REJECTED
                self._set_order_status_msg(order, "持仓不足")
                self._set_order_extra(order, {"status_msg": "持仓不足", "qmt_status": "ORDER_JUNK"})
                self.order_reject_reason[orderid] = "insufficient_position"
                self.order_submit_time.pop(orderid, None)
                self.gateway.on_order(order)
                self.gateway.write_log(f"拒单：持仓不足 pos={pos_volume} volume={order.volume}")
                return order.vt_orderid

        if order.direction == Direction.LONG:
            estimate_price = self._get_effective_price(order.price)
            estimate_amount = estimate_price * order.volume
            estimate_fee = self.calculate_fee(
                trade_amount=estimate_amount,
                direction=order.direction
            )
            need_frozen = estimate_amount + estimate_fee
            available_cash = self.capital - self.frozen
            if need_frozen > available_cash:
                order.status = Status.REJECTED
                order.status_msg = "260200:可用资金不足"
                self._set_order_extra(order, {"status_msg": "260200:可用资金不足", "qmt_status": "ORDER_JUNK"})
                self.order_reject_reason[orderid] = "insufficient_funds"
                self.order_submit_time.pop(orderid, None)
                self.gateway.on_order(order)
                self.gateway.write_log(
                    f"拒单：可用资金不足，可用={available_cash:.2f}，需冻结={need_frozen:.2f}"
                )
                return order.vt_orderid

            self.frozen += need_frozen
            self.order_frozen_cash[orderid] = need_frozen
            self.push_account()

        self.gateway.on_order(order)
        
        if order.status != Status.REJECTED:
            needs_scheduling = bool(case_tag) or self.fill_delay_ms > 0 or self.reporting_delay_ms > 0
            if needs_scheduling:
                base_dt = order.datetime
                report_at = base_dt + timedelta(milliseconds=int(self.reporting_delay_ms))
                timeout_override = None

                fill_at = None
                partial_at = None
                partial_ratio = 0.5
                status_msg = ""

                if case_tag.startswith("no_fill"):
                    if "_" in case_tag:
                        tail = case_tag.split("_")[-1].rstrip("s")
                        if tail.isdigit():
                            timeout_override = int(tail)
                    fill_at = None
                elif case_tag.startswith("delayed_fill_"):
                    secs_str = case_tag.replace("delayed_fill_", "").rstrip("s")
                    secs = int(secs_str) if secs_str.isdigit() else 5
                    fill_at = report_at + timedelta(seconds=secs)
                elif case_tag.startswith("partial_then_stall"):
                    secs = 1
                    if "_" in case_tag:
                        tail = case_tag.split("_")[-1].rstrip("s")
                        if tail.isdigit():
                            secs = int(tail)
                    partial_at = report_at + timedelta(seconds=secs)
                elif case_tag.startswith("force_reject"):
                    status_msg = "模拟强制拒单"
                else:
                    if self.fill_delay_ms > 0:
                        fill_at = report_at + timedelta(milliseconds=int(self.fill_delay_ms))

                if timeout_override and timeout_override > 0:
                    self._set_order_extra(order, {"timeout_seconds": timeout_override})

                self.order_tasks[orderid] = {
                    "case_tag": case_tag,
                    "phase": "unreported",
                    "report_at": report_at,
                    "fill_at": fill_at,
                    "partial_at": partial_at,
                    "partial_ratio": partial_ratio,
                    "status_msg": status_msg,
                }
            else:
                self.match_order(order)
        
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest):
        order = self.orders.get(req.orderid)
        if not order:
            return
            
        if order.status in [Status.ALLTRADED, Status.CANCELLED, Status.REJECTED]:
            return

        self.release_order_frozen_cash(order.orderid)
        order.status = Status.CANCELLED
        self.order_submit_time.pop(order.orderid, None)
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
                self.release_order_frozen_cash(order.orderid)
                order.status = Status.REJECTED
                order.status_msg = "模拟随机拒单"
                self.order_reject_reason[order.orderid] = "random_reject"
                self.order_submit_time.pop(order.orderid, None)
                self.gateway.on_order(order)
                # 拒单后不生成成交，不更新持仓/账户
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
            datetime=order.datetime,
            gateway_name=self.gateway.gateway_name
        )
        self.trades[trade.tradeid] = trade
        
        order.traded += trade_volume
        if order.traded >= order.volume:
            order.status = Status.ALLTRADED
            self.order_submit_time.pop(order.orderid, None)
        else:
            order.status = Status.PARTTRADED
            
        self.gateway.on_order(order)
        self.gateway.on_trade(trade)

        try:
            extra = getattr(order, "extra", None)
            case_tag = ""
            if isinstance(extra, dict):
                case_tag = str(extra.get("case_tag") or "")
            if case_tag:
                self.gateway.write_log(f"模拟成交触发: {order.vt_orderid} case={case_tag} traded={order.traded}/{order.volume} status={order.status}")
        except Exception:
            pass

        self.update_position(trade)
        self.update_account(trade)

    def _get_effective_price(self, price: float) -> float:
        if price > 0:
            return price
        return 10.0

    def calculate_fee(self, trade_amount: float, direction: Direction) -> float:
        commission = max(trade_amount * self.commission_rate, self.min_commission)
        transfer_fee = trade_amount * self.transfer_fee_rate
        stamp_duty = trade_amount * self.stamp_duty_rate if direction == Direction.SHORT else 0.0
        return commission + transfer_fee + stamp_duty

    def release_order_frozen_cash(
        self,
        orderid: str,
        release_amount: float = 0.0,
        push_event: bool = True
    ) -> None:
        frozen_cash = self.order_frozen_cash.get(orderid, 0.0)
        if frozen_cash <= 0:
            return

        amount = release_amount if release_amount > 0 else frozen_cash
        amount = min(amount, frozen_cash)
        self.frozen -= amount
        if self.frozen < 0:
            self.frozen = 0.0

        remain = frozen_cash - amount
        if remain > 0:
            self.order_frozen_cash[orderid] = remain
        else:
            self.order_frozen_cash.pop(orderid, None)
            self.order_submit_time.pop(orderid, None)
            self.order_reject_reason.pop(orderid, None)

        if push_event:
            self.push_account()

    def push_account(self) -> None:
        account = AccountData(
            accountid=self.accountid,
            balance=self.capital,
            frozen=self.frozen,
            gateway_name=self.gateway.gateway_name
        )
        self.accounts[account.accountid] = account
        self.gateway.on_account(account)

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
            print(f'创建新持仓{pos.vt_positionid}')
        if trade.direction == Direction.LONG:
            pos.volume += trade.volume
        else:
            pos.volume -= trade.volume
            
        # Ensure volume not negative
        if pos.volume < 0:
            pos.volume = 0

        # TODO 实际是不对的，这里只是模拟测试，看看成交逻辑，实际价格应该实时的价格，这里只提供成交的价格
        pos.price = trade.price
            
        self.gateway.on_position(pos)

    def update_account(self, trade: TradeData):
        trade_amount = trade.price * trade.volume
        trade_fee = self.calculate_fee(trade_amount, trade.direction)

        if trade.direction == Direction.LONG:
            self.capital -= (trade_amount + trade_fee)

            order = self.orders.get(trade.orderid)
            release_price = trade.price
            if order:
                release_price = self._get_effective_price(order.price)

            release_amount = release_price * trade.volume + self.calculate_fee(
                trade_amount=release_price * trade.volume,
                direction=Direction.LONG
            )
            self.release_order_frozen_cash(trade.orderid, release_amount, push_event=False)
        else:
            self.capital += (trade_amount - trade_fee)

        if self.capital < 0:
            self.capital = 0.0

        self.push_account()


class QmtSimTd:
    """
    QMT模拟交易接口
    """

    def __init__(self, gateway: BaseGateway):
        self.gateway = gateway
        self.gateway_name = gateway.gateway_name
        self.counter = SimulationCounter(gateway)

    def connect(self, setting: dict):
        acc_id = setting.get("账户", "test_id")
        account = AccountData(
            accountid=acc_id,
            balance=self.counter.capital,
            frozen=self.counter.frozen,
            gateway_name=self.gateway_name
        )
        self.counter.accounts[acc_id] = account
        self.counter.capital = setting.get("模拟资金", 10000000.0)
        self.counter.partial_rate = setting.get("部分成交率", 0.0)
        self.counter.reject_rate = setting.get("拒单率", 0.0)
        
        self.gateway.write_log("模拟交易接口连接成功")
        
        # 推送初始账户
        self.counter.accountid = account.accountid
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
