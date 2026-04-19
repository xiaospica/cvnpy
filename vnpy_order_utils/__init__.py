"""vnpy_order_utils — 订单生命周期共享工具包.

当前模块:
  * auto_resubmit.AutoResubmitMixin: 撤单/拒单自动重挂 mixin
  * pricing.choose_order_price: 按五档 + 涨跌停计算挂单价
  * pricing.convert_code_to_vnpy_type: 股票代码 → vnpy vt_symbol
  * protocols.OrderResubmitHost: Mixin 宿主类型锚 (Protocol)

设计原则:
  * 纯工具, 不引入 vnpy app 依赖, 只依赖 vnpy.trader.*
  * 面向多 app 复用 (signal_strategy_plus + ml_strategy + 未来其他)
  * Protocol 声明 mixin 对宿主的反向契约, mypy/Pylance 可校验
"""

from .auto_resubmit import AutoResubmitMixin
from .pricing import choose_order_price, convert_code_to_vnpy_type
from .protocols import OrderResubmitHost

__all__ = [
    "AutoResubmitMixin",
    "choose_order_price",
    "convert_code_to_vnpy_type",
    "OrderResubmitHost",
]
