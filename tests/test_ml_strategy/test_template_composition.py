"""Phase B — MLStrategyTemplate 脱离 SignalTemplatePlus + 组合 AutoResubmitMixin."""

from __future__ import annotations

from abc import ABC
from unittest.mock import MagicMock

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import OrderData

from vnpy_order_utils import AutoResubmitMixin
from vnpy_ml_strategy.template import MLStrategyTemplate


class _ConcreteMLStrategy(MLStrategyTemplate):
    """Minimal concrete subclass for instantiation testing."""

    def generate_orders(self, selected):
        pass


def test_mro_mixin_first():
    """AutoResubmitMixin 必须在 MRO 的 ABC 之前, 且不含 SignalTemplatePlus."""
    mro = [c.__name__ for c in MLStrategyTemplate.__mro__]
    assert "AutoResubmitMixin" in mro
    assert "SignalTemplatePlus" not in mro
    # mixin 先于 ABC, 确保它的 __init__ 先跑
    assert mro.index("AutoResubmitMixin") < mro.index("ABC")


def test_instantiation_initializes_mixin_state():
    fake_engine = MagicMock()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="test")

    # AutoResubmitMixin.__init__ 应该已经初始化了 3 个字典
    assert strat._resubmit_count == {}
    assert strat._pending_resubmit == {}
    assert strat._resubmit_clock == 0
    # 自己的 strategy_name / signal_engine 也到位
    assert strat.strategy_name == "test"
    assert strat.signal_engine is fake_engine


def test_send_order_refuses_when_not_trading():
    fake_engine = MagicMock()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="dry")
    strat.gateway = "QMT_SIM"
    # trading=False by default
    result = strat.send_order(
        vt_symbol="600000.SSE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=10.0,
        volume=100,
        order_type=OrderType.LIMIT,
    )
    assert result == []
    # main_engine.send_order 不应被调用
    fake_engine.main_engine.send_order.assert_not_called()


def test_send_order_requires_gateway():
    fake_engine = MagicMock()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="dry")
    strat.trading = True
    # gateway 为空
    result = strat.send_order(
        vt_symbol="600000.SSE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=10.0, volume=100,
    )
    assert result == []


def test_send_order_rejects_malformed_vt_symbol():
    fake_engine = MagicMock()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="dry")
    strat.trading = True
    strat.gateway = "QMT_SIM"
    # malformed vt_symbol (no exchange suffix)
    result = strat.send_order(
        vt_symbol="no_exchange",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=10.0, volume=100,
    )
    assert result == []


def test_send_order_tracks_orderid_on_success():
    fake_main = MagicMock()
    fake_main.send_order.return_value = "QMT_SIM.42"
    fake_engine = MagicMock(main_engine=fake_main)
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="s1")
    strat.trading = True
    strat.gateway = "QMT_SIM"

    ids = strat.send_order(
        vt_symbol="600000.SSE",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        price=10.0, volume=100,
    )
    assert ids == ["QMT_SIM.42"]
    fake_engine.track_order.assert_called_once_with("QMT_SIM.42", "s1")


def test_on_order_delegates_to_mixin():
    """on_order 应调 on_order_for_resubmit (mixin 契约)."""
    from datetime import datetime
    from vnpy.trader.constant import Exchange

    # Use fake signal_engine WITHOUT main_engine so adjust_resubmit_price
    # 走 fallback 路径, 避免 Mock 的 Decimal 问题
    class _Engine:
        pass
    fake_engine = _Engine()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="s1")

    order = OrderData(
        symbol="600000", exchange=Exchange.SSE, orderid="1",
        direction=Direction.LONG, offset=Offset.OPEN, type=OrderType.LIMIT,
        price=10.0, volume=100, traded=0,
        status=Status.CANCELLED,
        datetime=datetime.now(),
        gateway_name="QMT_SIM",
    )

    strat.on_order(order)
    assert order.vt_orderid in strat._pending_resubmit
    assert strat._pending_resubmit[order.vt_orderid]["reason"] == "cancel"


def test_on_timer_delegates_to_mixin_throttles():
    """on_timer 应调 on_timer_for_resubmit, 空队列时直接返回."""
    fake_engine = MagicMock()
    strat = _ConcreteMLStrategy(fake_engine, strategy_name="s1")
    # 初始 clock=0, resubmit_interval=5 → 第 5 次 tick 才会处理
    for _ in range(5):
        strat.on_timer()
    assert strat._resubmit_clock == 5
    # 队列空, 没有副作用
    assert strat._pending_resubmit == {}


def test_ismixin_contract_uses_order_utils_class():
    """确认 mixin 来自 vnpy_order_utils 而非 signal_strategy_plus 的 shim."""
    from vnpy_order_utils.auto_resubmit import AutoResubmitMixin as CanonicalMixin

    assert AutoResubmitMixin is CanonicalMixin
    assert issubclass(_ConcreteMLStrategy, CanonicalMixin)
