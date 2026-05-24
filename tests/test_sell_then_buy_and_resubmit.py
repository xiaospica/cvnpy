from datetime import datetime, timedelta

import pytest

import vnpy_signal_strategy_plus.mysql_signal_strategy as mysql_mod
from vnpy_signal_strategy_plus.auto_resubmit import AutoResubmitMixinPlus
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import AccountData, OrderData


class _DummySignalEngine:
    def __init__(self, main_engine):
        self.main_engine = main_engine

    def write_log(self, msg: str, strategy) -> None:
        return


class _DummyMainEngine:
    def __init__(self, accounts=None, gateway=None):
        self._accounts = accounts or []
        self._gateway = gateway
        self.gateways = {}

    def get_all_accounts(self):
        return self._accounts

    def get_gateway(self, gateway_name: str):
        return self._gateway


class _DummyResubmit(AutoResubmitMixinPlus):
    def write_log(self, msg: str) -> None:
        return

    def send_order(self, *args, **kwargs):
        return []


def test_should_auto_resubmit_only_cancelled() -> None:
    s = _DummyResubmit()

    base = dict(
        symbol="600000",
        exchange=Exchange.SSE,
        orderid="1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        type=OrderType.LIMIT,
        price=10.0,
        volume=100,
        traded=0,
        datetime=datetime.now(),
        gateway_name="TEST",
    )

    cancelled = OrderData(status=Status.CANCELLED, **base)
    rejected_sell = OrderData(status=Status.REJECTED, **{**base, "direction": Direction.SHORT})
    rejected_buy = OrderData(status=Status.REJECTED, **{**base, "direction": Direction.LONG})
    rejected_buy.extra = {"status_msg": "[260200][可用资金不足]"}  # type: ignore[attr-defined]

    assert s.should_auto_resubmit(cancelled) is True
    assert s.should_auto_resubmit(rejected_sell) is False
    assert s.should_auto_resubmit(rejected_buy) is True


def test_reject_resubmit_is_delayed() -> None:
    s = _DummyResubmit()
    s.resubmit_interval = 1
    s.reject_resubmit_delay_seconds = 5

    order = OrderData(
        symbol="600000",
        exchange=Exchange.SSE,
        orderid="1",
        direction=Direction.LONG,
        offset=Offset.OPEN,
        type=OrderType.LIMIT,
        price=10.0,
        volume=100,
        traded=0,
        status=Status.REJECTED,
        datetime=datetime.now(),
        gateway_name="TEST",
    )
    order.extra = {"status_msg": "[260200][可用资金不足]"}  # type: ignore[attr-defined]

    s.on_order_for_resubmit(order)
    task = s._pending_resubmit.get(order.vt_orderid)
    assert task is not None
    assert task["reason"] == "reject_insufficient_cash"
    assert task["ready_at"] > datetime.now()


def test_get_account_asset_uses_equity_balance() -> None:
    accounts = [AccountData(accountid="ACC", balance=12345.0, frozen=0.0, gateway_name="G1")]
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine(accounts)))
    assert st.get_account_asset("G1") == 12345.0


def test_signal_pct_tiny_overflow_is_clamped() -> None:
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine()))

    assert st.normalize_signal_pct(1.0002) == 1.0
    assert st.normalize_signal_pct(1.001) == 1.0
    assert st.normalize_signal_pct(1.0011) is None


def test_near_full_buy_volume_is_capped_by_available_cash() -> None:
    account = AccountData(
        accountid="ACC",
        balance=1_001_261.96,
        frozen=0.0,
        gateway_name="G1",
    )
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine([account])))

    capped = st.cap_full_buy_volume_by_cash(
        gateway_name="G1",
        price=1.508,
        requested_volume=663_900,
        pct=1.0,
    )

    assert capped == 663_800
    assert st.estimate_buy_frozen_cash("G1", 1.508, capped) <= account.available
    assert st.estimate_buy_frozen_cash("G1", 1.508, 663_900) > account.available


def test_non_full_buy_volume_is_not_cash_capped() -> None:
    account = AccountData(accountid="ACC", balance=1_000.0, frozen=0.0, gateway_name="G1")
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine([account])))

    assert st.cap_full_buy_volume_by_cash(
        gateway_name="G1",
        price=10.0,
        requested_volume=10_000,
        pct=0.5,
    ) == 10_000


def test_tiny_sell_pct_rounds_to_one_board_lot_when_available() -> None:
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine()))

    assert st.adjust_sell_volume_by_available_position(
        vt_symbol="588080.SSE",
        raw_volume=65.4,
        rounded_volume=0,
        available=500,
        empty_signal=False,
    ) == 100


def test_tiny_sell_pct_stays_zero_without_one_board_lot_available() -> None:
    st = mysql_mod.MySQLSignalStrategyPlus(_DummySignalEngine(_DummyMainEngine()))

    assert st.adjust_sell_volume_by_available_position(
        vt_symbol="588080.SSE",
        raw_volume=65.4,
        rounded_volume=0,
        available=50,
        empty_signal=False,
    ) == 0


def test_replay_get_account_asset_reads_sim_counter_not_stale_oms() -> None:
    from vnpy_signal_strategy_plus.strategies.csv_replay_test_strategy import (
        CsvReplayTestStrategy,
    )

    class _Pos:
        volume = 20
        price = 10.0

    class _Counter:
        capital = 1000.0
        positions = {"600000.SSE.Long": _Pos()}

    class _Gateway:
        md = object()

        class _Td:
            counter = _Counter()

        td = _Td()

        def enable_auto_settle(self, enabled: bool) -> None:
            return

    class _ReplayMainEngine(_DummyMainEngine):
        def __init__(self):
            super().__init__([
                AccountData(accountid="ACC", balance=1.0, frozen=0.0, gateway_name="G1")
            ])
            self._gateway = _Gateway()

        def get_gateway(self, gateway_name: str):
            return self._gateway if gateway_name == "G1" else None

    st = object.__new__(CsvReplayTestStrategy)
    st.gateway = "G1"
    st.signal_engine = _DummySignalEngine(_ReplayMainEngine())
    st.write_log = lambda msg: None

    assert st.get_account_asset("G1") == 1200.0


def test_resubmit_send_order_clears_live_test_case_tag() -> None:
    class _S(AutoResubmitMixinPlus):
        def __init__(self) -> None:
            super().__init__()
            self.is_live_test_strategy = True
            self.live_test_order_tag = "reject_down"
            self.sent_with_tag = None

        def write_log(self, msg: str) -> None:
            return

        def send_order(self, *args, **kwargs):
            self.sent_with_tag = self.live_test_order_tag
            return ["QMT_SIM.100"]

    s = _S()
    task = {
        "vt_symbol": "510300.SSE",
        "direction": Direction.LONG,
        "offset": Offset.OPEN,
        "order_type": OrderType.LIMIT,
        "price": 4.0,
        "volume": 100.0,
        "attempts": 0,
        "reason": "cancel",
        "ready_at": datetime.now() - timedelta(seconds=1),
        "reject_msg": "",
    }
    s._pending_resubmit["QMT_SIM.1"] = task
    s._process_single_resubmit_task("QMT_SIM.1", task)

    assert s.sent_with_tag == ""
    assert s.live_test_order_tag == "reject_down"
