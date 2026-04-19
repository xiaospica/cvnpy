"""Backward-compat shim — canonical 位置是 ``vnpy_order_utils.pricing``.

``choose_order_price`` + ``convert_code_to_vnpy_type`` 已迁到 vnpy_order_utils,
本文件只做 re-export 保证 signal app 现有的 import 路径可用, 以及测试文件
``tests/test_signal_pricing.py`` 继续工作.

新代码请直接 ``from vnpy_order_utils.pricing import choose_order_price``.
"""

from vnpy_order_utils.pricing import choose_order_price, convert_code_to_vnpy_type

__all__ = ["choose_order_price", "convert_code_to_vnpy_type"]
