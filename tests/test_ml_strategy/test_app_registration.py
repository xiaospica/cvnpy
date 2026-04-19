"""Phase 2.1 — app 注册 + 引擎 lifecycle."""

from __future__ import annotations

from datetime import date

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from vnpy_ml_strategy import APP_NAME, MLStrategyApp


def test_app_register_into_main_engine():
    ev = EventEngine()
    main = MainEngine(ev)
    main.add_app(MLStrategyApp)
    eng = main.get_engine(APP_NAME)
    assert eng is not None
    assert eng.scheduler is not None
    main.close()


def test_is_trade_day_fallback_without_calendar():
    """未注入 trade_calendar 时 fallback 到 weekday<5."""
    ev = EventEngine()
    main = MainEngine(ev)
    main.add_app(MLStrategyApp)
    eng = main.get_engine(APP_NAME)
    # sanity: Monday
    assert eng.is_trade_day(date(2026, 4, 20))
    # Sunday
    assert not eng.is_trade_day(date(2026, 4, 19))
    main.close()
