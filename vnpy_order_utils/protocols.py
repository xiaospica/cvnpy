"""AutoResubmitMixin 对宿主类的反向契约 — 类型锚,不是 runtime 强制.

宿主类(``SignalTemplatePlus`` / 未来的 ``MLBaseTemplate`` / …) 需要提供:
  * ``send_order(vt_symbol, direction, offset, price, volume, order_type) -> List[str]``
  * ``write_log(msg: str) -> None``
  * ``signal_engine`` 属性,任何具有 ``.main_engine`` 属性的对象(鸭子类型)
  * ``gateway`` 属性,字符串,用于 ``choose_order_price`` 的 gateway 查询兜底

Mixin 内部会在宿主上读写的状态字段(重要:这些是外部 API,子类可能读取):
  * ``_is_resubmitting: bool`` — 重挂发单期间置 True,``get_order_reference`` 等钩子
    可判断是否在重挂语境中(参考 live_order_test_strategy.py 有消费)
  * ``_resubmit_count: Dict[str, int]`` — per-orderid 重试次数
  * ``_pending_resubmit: Dict[str, dict]`` — 等待重挂的任务队列
  * ``_resubmit_clock: int`` — 定时器 tick 计数,用于节流

使用方式:
  mypy / Pylance 可以用 ``self: OrderResubmitHost`` 的 typing 方式在 mixin 内
  增强类型推导; runtime 不检查 — 只要宿主实现了上述四个公开接口就能跑.
"""

from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable


@runtime_checkable
class OrderResubmitHost(Protocol):
    """Host 类必须暴露的最小接口集合."""

    gateway: str
    signal_engine: Any  # 鸭子: 需要有 .main_engine 属性

    def send_order(
        self,
        vt_symbol: str,
        direction: Any,
        offset: Any,
        price: float,
        volume: float,
        order_type: Any,
    ) -> List[str]:
        ...

    def write_log(self, msg: str) -> None:
        ...
