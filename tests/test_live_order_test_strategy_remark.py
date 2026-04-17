from datetime import datetime

from vnpy_signal_strategy_plus.base import EngineType
from vnpy_signal_strategy_plus.strategies.live_order_test_strategy import LiveOrderTestStrategyPlus


class _DummyMainEngine:
    pass


class _DummySignalEngine:
    def __init__(self):
        self.main_engine = _DummyMainEngine()

    def write_log(self, msg: str, strategy):
        return


def test_live_test_remark_aligns_current_dt_date() -> None:
    st = LiveOrderTestStrategyPlus(_DummySignalEngine())
    st.engine_type = EngineType.BACKTESTING.value
    st.current_dt = datetime(2026, 3, 25, 23, 59, 59, 999999)

    base = st.get_test_remark_base()
    assert base.tzinfo is None
    assert base.date() == st.current_dt.date()
    assert base.time().hour == 0 and base.time().minute == 0 and base.time().second == 1

