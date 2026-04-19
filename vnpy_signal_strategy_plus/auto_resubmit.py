"""Backward-compat shim — canonical 位置是 ``vnpy_order_utils.auto_resubmit``.

原先所有子类都 ``from vnpy_signal_strategy_plus.auto_resubmit import AutoResubmitMixinPlus``;
抽包后类被改名为 ``AutoResubmitMixin`` 放在 ``vnpy_order_utils``, 本 shim 保留
``AutoResubmitMixinPlus`` 这个历史类别名, 现有 import + 类变量覆写全部可
继续工作.

不要往这个 shim 里加新逻辑 — 新代码直接 import 自 vnpy_order_utils.
"""

from vnpy_order_utils.auto_resubmit import AutoResubmitMixin as AutoResubmitMixinPlus

__all__ = ["AutoResubmitMixinPlus"]
