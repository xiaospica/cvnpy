from vnpy.trader.object import OrderData
from vnpy.trader.constant import Status


class AutoResubmitMixinPlus:
    """策略层撤单/拒单重挂混入能力。"""

    resubmit_limit: int = 10
    resubmit_interval: int = 5

    def __init__(self, *args, **kwargs):
        """初始化重挂状态容器。"""
        super().__init__(*args, **kwargs)
        self._resubmit_count: dict[str, int] = {}
        self._pending_resubmit: dict[str, dict] = {}
        self._resubmit_clock: int = 0

    def should_auto_resubmit(self, order: OrderData) -> bool:
        """判断订单是否满足自动重挂条件。"""
        if order.status not in (Status.CANCELLED, Status.REJECTED):
            return False
        if order.traded >= order.volume:
            return False
        if self._resubmit_count.get(order.vt_orderid, 0) >= self.resubmit_limit:
            return False
        return True

    def adjust_resubmit_price(self, order: OrderData) -> float:
        """计算重挂价格，默认沿用原委托价。"""
        return order.price

    def on_order_for_resubmit(self, order: OrderData) -> None:
        """在订单回报中登记待重挂任务，不在回调内直接重提。"""
        if not self.should_auto_resubmit(order):
            return
        if order.vt_orderid in self._pending_resubmit:
            return
        self.write_log(f"触发撤单重挂：{order}")
        remain: float = order.volume - order.traded
        if remain <= 0:
            return
        attempts: int = self._resubmit_count.get(order.vt_orderid, 0)
        self._pending_resubmit[order.vt_orderid] = {
            "vt_symbol": order.vt_symbol,
            "direction": order.direction,
            "offset": order.offset,
            "order_type": order.type,
            "price": self.adjust_resubmit_price(order),
            "volume": remain,
            "attempts": attempts,
        }
        self.write_log(f"加入重挂队列: {order.vt_orderid} 剩余={remain}")

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
            attempts: int = int(task["attempts"])
            if attempts >= self.resubmit_limit:
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
                continue

            next_attempt: int = attempts + 1
            for new_vt_orderid in vt_orderids:
                self._resubmit_count[new_vt_orderid] = next_attempt

            self._pending_resubmit.pop(vt_orderid, None)
            self.write_log(f"重挂已提交: 原={vt_orderid} 新={vt_orderids}")
