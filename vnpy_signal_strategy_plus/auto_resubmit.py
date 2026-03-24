from datetime import datetime, timedelta

from vnpy.trader.object import OrderData
from vnpy.trader.constant import Status, Direction
from .utils import choose_order_price


class AutoResubmitMixinPlus:
    """策略层撤单/拒单重挂混入能力。"""

    resubmit_limit: int = 10
    resubmit_interval: int = 5
    reject_resubmit_limit: int = 3
    reject_resubmit_delay_seconds: int = 5
    reject_resubmit_backoff_max_seconds: int = 60

    def __init__(self, *args, **kwargs):
        """初始化重挂状态容器。"""
        super().__init__(*args, **kwargs)
        self._resubmit_count: dict[str, int] = {}
        self._pending_resubmit: dict[str, dict] = {}
        self._resubmit_clock: int = 0

    def get_reject_status_msg(self, order: OrderData) -> str:
        msg = ""
        try:
            extra = getattr(order, "extra", None)
            if isinstance(extra, dict):
                msg = str(extra.get("status_msg") or "")
        except Exception:
            msg = ""

        if not msg:
            msg = str(getattr(order, "status_msg", "") or "")
        return msg

    def is_insufficient_cash_reject(self, order: OrderData) -> bool:
        msg = self.get_reject_status_msg(order)
        if not msg:
            return False
        if "260200" in msg:
            return True
        if "可用资金不足" in msg:
            return True
        if "资金不足" in msg:
            return True
        return False

    def should_auto_resubmit(self, order: OrderData) -> bool:
        """判断订单是否满足自动重挂条件。"""
        if order.status == Status.CANCELLED:
            pass
        elif order.status == Status.REJECTED:
            if order.direction != Direction.LONG:
                return False
            if not self.is_insufficient_cash_reject(order):
                return False
        else:
            return False
        if order.traded >= order.volume:
            return False
        attempts = self._resubmit_count.get(order.vt_orderid, 0)
        if order.status == Status.REJECTED:
            if attempts >= self.reject_resubmit_limit:
                return False
        elif attempts >= self.resubmit_limit:
            return False
        return True

    def adjust_resubmit_price(self, order: OrderData) -> float:
        """计算重挂价格，优先按最新五档买一/卖一重新定价。"""
        main_engine = getattr(self, "signal_engine", None)
        if not main_engine:
            return float(order.price)

        engine = getattr(main_engine, "main_engine", None)
        if not engine:
            return float(order.price)

        gateway = None
        if getattr(order, "gateway_name", ""):
            gateway = engine.get_gateway(order.gateway_name)
        if not gateway and getattr(self, "gateway", ""):
            gateway = engine.get_gateway(self.gateway)

        tick = None
        if gateway and hasattr(gateway, "get_full_tick"):
            try:
                tick = gateway.get_full_tick(order.vt_symbol)
            except Exception:
                tick = None

        if not tick:
            tick = engine.get_tick(order.vt_symbol)

        contract = engine.get_contract(order.vt_symbol)
        pricetick = contract.pricetick if contract else None
        return choose_order_price(tick, order.direction, float(order.price), pricetick)

    def on_order_for_resubmit(self, order: OrderData) -> None:
        """在订单回报中登记待重挂任务，不在回调内直接重提。"""
        if not self.should_auto_resubmit(order):
            return
        if order.vt_orderid in self._pending_resubmit:
            return
        remain: float = order.volume - order.traded
        if remain <= 0:
            return
        attempts: int = self._resubmit_count.get(order.vt_orderid, 0)
        reason = "cancel"
        ready_at = datetime.now()
        reject_msg = ""

        if order.status == Status.REJECTED:
            reason = "reject_insufficient_cash"
            reject_msg = self.get_reject_status_msg(order)
            delay = max(int(self.reject_resubmit_delay_seconds), 1)
            delay = min(delay * (2**attempts), int(self.reject_resubmit_backoff_max_seconds))
            ready_at = datetime.now() + timedelta(seconds=delay)
            self.write_log(f"触发拒单延时重挂：{order.vt_orderid} delay={delay}s msg={reject_msg}")
        else:
            self.write_log(f"触发撤单重挂：{order.vt_orderid}")

        self._pending_resubmit[order.vt_orderid] = {
            "vt_symbol": order.vt_symbol,
            "direction": order.direction,
            "offset": order.offset,
            "order_type": order.type,
            "price": self.adjust_resubmit_price(order),
            "volume": remain,
            "attempts": attempts,
            "reason": reason,
            "ready_at": ready_at,
            "reject_msg": reject_msg,
        }
        self.write_log(f"加入重挂队列: {order.vt_orderid} reason={reason} ready_at={ready_at} 剩余={remain}")

    def on_timer_for_resubmit(self) -> None:
        """按定时节流处理重挂队列并提交新委托。"""
        self._resubmit_clock += 1
        if self.resubmit_interval <= 0:
            self.resubmit_interval = 1
        if self._resubmit_clock % self.resubmit_interval != 0:
            return
        if not self._pending_resubmit:
            return

        for vt_orderid, task in list(self._pending_resubmit.items()):
            ready_at = task.get("ready_at")
            if isinstance(ready_at, datetime):
                if datetime.now() < ready_at:
                    continue

            attempts: int = int(task["attempts"])
            limit = self.resubmit_limit
            if task.get("reason") == "reject_insufficient_cash":
                limit = self.reject_resubmit_limit
            if attempts >= limit:
                self._pending_resubmit.pop(vt_orderid, None)
                self.write_log(f"重挂达到上限，放弃: {vt_orderid}")
                continue

            vt_orderids = self.send_order(
                vt_symbol=task["vt_symbol"],
                direction=task["direction"],
                offset=task["offset"],
                price=float(task["price"]),
                volume=float(task["volume"]),
                order_type=task["order_type"],
            )

            if not vt_orderids:
                task["attempts"] = attempts + 1
                if task.get("reason") == "reject_insufficient_cash":
                    delay = max(int(self.reject_resubmit_delay_seconds), 1)
                    delay = min(delay * (2 ** int(task["attempts"])), int(self.reject_resubmit_backoff_max_seconds))
                    task["ready_at"] = datetime.now() + timedelta(seconds=delay)
                continue

            next_attempt: int = attempts + 1
            for new_vt_orderid in vt_orderids:
                self._resubmit_count[new_vt_orderid] = next_attempt

            self._pending_resubmit.pop(vt_orderid, None)
            self.write_log(f"重挂已提交: 原={vt_orderid} 新={vt_orderids}")
