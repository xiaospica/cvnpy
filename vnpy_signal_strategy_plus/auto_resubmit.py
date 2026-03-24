from datetime import datetime, timedelta
from typing import Any, Dict

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

    def __init__(self, *args: Any, **kwargs: Any):
        """初始化重挂状态容器。"""
        super().__init__(*args, **kwargs)
        self._resubmit_count: Dict[str, int] = {}
        self._pending_resubmit: Dict[str, dict] = {}
        self._resubmit_clock: int = 0

    def get_reject_status_msg(self, order: OrderData) -> str:
        """获取订单被拒单时的状态信息"""
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
        """判断是否为可用资金不足导致的拒单"""
        msg = self.get_reject_status_msg(order)
        if not msg:
            return False
        
        # 常见资金不足的拒单关键词或错误码
        if "260200" in msg or "可用资金不足" in msg or "资金不足" in msg:
            return True
        return False

    def should_auto_resubmit(self, order: OrderData) -> bool:
        """判断订单是否满足自动重挂条件。"""
        # 仅处理撤单和特定情况下的拒单
        if order.status == Status.CANCELLED:
            pass
        elif order.status == Status.REJECTED:
            # 目前只针对买单因为资金不足被拒进行重试
            if order.direction != Direction.LONG:
                return False
            if not self.is_insufficient_cash_reject(order):
                return False
        else:
            return False

        # 如果已经全部成交，则无需重挂
        if order.traded >= order.volume:
            return False

        # 检查重挂次数是否达到上限
        attempts = self._resubmit_count.get(order.vt_orderid, 0)
        if order.status == Status.REJECTED:
            if attempts >= self.reject_resubmit_limit:
                self.write_log(f"【重挂拦截】拒单重试次数达到上限，拦截重挂: {order.vt_orderid}, attempts={attempts}")
                return False
        elif attempts >= self.resubmit_limit:
            self.write_log(f"【重挂拦截】撤单重试次数达到上限，拦截重挂: {order.vt_orderid}, attempts={attempts}")
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

        # 尝试获取对应网关实例
        gateway = None
        gateway_name = getattr(order, "gateway_name", "") or getattr(self, "gateway", "")
        if gateway_name:
            gateway = engine.get_gateway(gateway_name)

        # 尝试通过主动拉取五档行情进行定价
        tick = None
        if gateway and hasattr(gateway, "get_full_tick"):
            try:
                tick = gateway.get_full_tick(order.vt_symbol)
            except Exception as e:
                self.write_log(f"【重挂定价】获取五档行情异常: {order.vt_symbol}, error={e}")
                tick = None

        # 退化使用主引擎缓存的Tick数据
        if not tick:
            tick = engine.get_tick(order.vt_symbol)

        contract = engine.get_contract(order.vt_symbol)
        pricetick = contract.pricetick if contract else None
        
        # 利用统一的盘口定价工具计算新价格
        new_price = choose_order_price(tick, order.direction, float(order.price), pricetick)
        self.write_log(f"【重挂定价】{order.vt_orderid} 原价格={order.price}, 新价格={new_price}")
        return new_price

    def on_order_for_resubmit(self, order: OrderData) -> None:
        """在订单回报中登记待重挂任务，不在回调内直接重提以避免死循环。"""
        if not self.should_auto_resubmit(order):
            return
            
        if order.vt_orderid in self._pending_resubmit:
            self.write_log(f"【重挂】订单已在重挂队列中，跳过: {order.vt_orderid}")
            return
            
        remain: float = order.volume - order.traded
        if remain <= 0:
            return
            
        attempts: int = self._resubmit_count.get(order.vt_orderid, 0)
        reason = "cancel"
        ready_at = datetime.now()
        reject_msg = ""

        # 对于拒单，应用指数退避的延时重挂策略
        if order.status == Status.REJECTED:
            reason = "reject_insufficient_cash"
            reject_msg = self.get_reject_status_msg(order)
            delay = max(int(self.reject_resubmit_delay_seconds), 1)
            delay = min(delay * (2**attempts), int(self.reject_resubmit_backoff_max_seconds))
            ready_at = datetime.now() + timedelta(seconds=delay)
            self.write_log(f"【重挂】触发资金不足拒单延时重挂：{order.vt_orderid} delay={delay}s msg={reject_msg}")
        else:
            self.write_log(f"【重挂】触发撤单重挂：{order.vt_orderid}")

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
        self.write_log(f"【重挂】加入重挂队列成功: {order.vt_orderid} reason={reason} ready_at={ready_at} 剩余={remain} attempts={attempts}")

    def on_timer_for_resubmit(self) -> None:
        """按定时节流处理重挂队列并提交新委托。"""
        self._resubmit_clock += 1
        if self.resubmit_interval <= 0:
            self.resubmit_interval = 1
            
        if self._resubmit_clock % self.resubmit_interval != 0:
            return
            
        if not self._pending_resubmit:
            return

        # 遍历当前等待重挂的订单
        for vt_orderid, task in list(self._pending_resubmit.items()):
            self._process_single_resubmit_task(vt_orderid, task)

    def _process_single_resubmit_task(self, vt_orderid: str, task: dict) -> None:
        """处理单一的重挂任务，提交新订单或进行退避。"""
        ready_at = task.get("ready_at")
        if isinstance(ready_at, datetime):
            if datetime.now() < ready_at:
                return

        attempts: int = int(task["attempts"])
        limit = self.resubmit_limit
        if task.get("reason") == "reject_insufficient_cash":
            limit = self.reject_resubmit_limit
            
        if attempts >= limit:
            self._pending_resubmit.pop(vt_orderid, None)
            self.write_log(f"【重挂处理】重挂达到上限，放弃: {vt_orderid}, attempts={attempts}")
            return

        # 执行发单
        vt_orderids = self.send_order(
            vt_symbol=task["vt_symbol"],
            direction=task["direction"],
            offset=task["offset"],
            price=float(task["price"]),
            volume=float(task["volume"]),
            order_type=task["order_type"],
        )

        # 发单失败，更新重试次数并重新计算延时
        if not vt_orderids:
            task["attempts"] = attempts + 1
            self.write_log(f"【重挂处理】send_order发单失败，增加尝试次数: {vt_orderid} -> attempts={task['attempts']}")
            
            if task.get("reason") == "reject_insufficient_cash":
                delay = max(int(self.reject_resubmit_delay_seconds), 1)
                delay = min(delay * (2 ** int(task["attempts"])), int(self.reject_resubmit_backoff_max_seconds))
                task["ready_at"] = datetime.now() + timedelta(seconds=delay)
                self.write_log(f"【重挂处理】更新下次重挂时间: {vt_orderid} delay={delay}s")
            return

        # 发单成功，记录新订单号和尝试次数
        next_attempt: int = attempts + 1
        for new_vt_orderid in vt_orderids:
            self._resubmit_count[new_vt_orderid] = next_attempt

        self._pending_resubmit.pop(vt_orderid, None)
        self.write_log(f"【重挂处理】重挂已提交: 原={vt_orderid} 新={vt_orderids}")
