from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from vnpy_signal_strategy_plus import replay_adapter as replay_adapter_module
from vnpy_signal_strategy_plus.replay_adapter import SignalJournalReplayAdapter
from vnpy_signal_strategy_plus.base import EngineType
from vnpy_signal_strategy_plus.mysql_signal_strategy import MySQLSignalStrategyPlus
from vnpy_signal_strategy_plus.strategies import csv_replay_test_strategy as csv_replay_module
from vnpy_signal_strategy_plus.strategies.csv_replay_test_strategy import CsvReplayTestStrategy
from vnpy_signal_strategy_plus.strategies.redis_live_sim_test_strategy import (
    RedisLiveSimTestStrategy,
)
from vnpy_signal_strategy_plus.signal_journal import (
    PCT_SEMANTICS,
    SignalJournalBase,
    StrategySignalApplication,
    TradeSignalEvent,
    normalize_trade_signal_payload,
    record_signal_application,
    upsert_trade_signal_event,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    SignalJournalBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_normalize_preserves_jq_pct_semantics_and_uid():
    payload = {
        "signal_uid": "jq:demo:1",
        "source_signal_id": "demo:2026-05-15 09:30:00:1",
        "code": "518880.SH",
        "pct": "0.1234",
        "pct_semantics": PCT_SEMANTICS,
        "type": "BUY_LST",
        "price": "6.5",
        "stg": "demo",
        "remark": "2026-05-15 09:30:00",
        "empty": "0",
        "amt": "1000",
    }

    normalized = normalize_trade_signal_payload(
        payload,
        target_stg="fallback",
        stream_key="demo",
        redis_id="1-0",
    )

    assert normalized["signal_uid"] == "jq:demo:1"
    assert normalized["stg"] == "demo"
    assert normalized["pct"] == 0.1234
    assert normalized["pct_semantics"] == PCT_SEMANTICS
    assert normalized["trade_date"] == "2026-05-15"


def test_signal_event_upsert_is_idempotent_by_uid():
    session = _session()
    normalized = normalize_trade_signal_payload(
        {
            "signal_uid": "jq:demo:stable",
            "code": "518880.SH",
            "pct": "0.1",
            "pct_semantics": PCT_SEMANTICS,
            "type": "BUY_LST",
            "price": "6.5",
            "stg": "demo",
            "remark": "2026-05-15 09:30:00",
        },
        target_stg="demo",
    )

    row1, created1 = upsert_trade_signal_event(session, normalized)
    row2, created2 = upsert_trade_signal_event(session, normalized)

    assert created1 is True
    assert created2 is False
    assert row1.id == row2.id
    assert session.query(TradeSignalEvent).count() == 1


def test_strategy_application_checkpoint_is_per_scope():
    session = _session()
    normalized = normalize_trade_signal_payload(
        {
            "signal_uid": "jq:demo:checkpoint",
            "code": "518880.SH",
            "pct": "0.1",
            "pct_semantics": PCT_SEMANTICS,
            "type": "BUY_LST",
            "price": "6.5",
            "stg": "demo",
            "remark": "2026-05-15 09:30:00",
        },
        target_stg="demo",
    )
    signal, _ = upsert_trade_signal_event(session, normalized)

    record_signal_application(
        session,
        signal_event_id=signal.id,
        account_id="QMT_SIM",
        gateway_name="QMT_SIM",
        engine="SignalStrategyPlus",
        strategy_name="demo",
        status="ordered",
        order_refs=["QMT_SIM.1"],
    )
    record_signal_application(
        session,
        signal_event_id=signal.id,
        account_id="QMT_SIM",
        gateway_name="QMT_SIM",
        engine="SignalStrategyPlus",
        strategy_name="demo",
        status="ordered",
        order_refs=["QMT_SIM.1"],
    )

    assert session.query(StrategySignalApplication).count() == 1
    app = session.query(StrategySignalApplication).one()
    assert app.status == "ordered"
    assert "QMT_SIM.1" in app.order_refs_json


def test_csv_replay_passes_final_settle_day_to_adapter(monkeypatch):
    captured = {}

    class DummyAdapter:
        def __init__(self, strategy, **kwargs):
            captured["strategy"] = strategy
            captured.update(kwargs)

        def run_polling_loop(self):
            captured["ran"] = True

    monkeypatch.setattr(
        csv_replay_module,
        "SignalJournalReplayAdapter",
        DummyAdapter,
    )
    strategy = CsvReplayTestStrategy.__new__(CsvReplayTestStrategy)
    strategy.gateway = "QMT_SIM"
    strategy._replay_enabled = True
    strategy._idle_settle_seconds = 1.5
    strategy._final_settle_day = date(2026, 5, 11)
    strategy._get_sim_gateway = lambda: object()
    strategy._is_replay_trade_day = lambda day: True
    strategy.write_log = lambda message: None

    strategy.run_polling()

    assert captured["ran"] is True
    assert captured["final_settle_day"] == date(2026, 5, 11)
    assert captured["idle_settle_seconds"] == 1.5


def test_redis_replay_explicit_settle_day_wins():
    strategy = RedisLiveSimTestStrategy.__new__(RedisLiveSimTestStrategy)

    day = strategy._resolve_final_settle_day(
        {},
        {"settle_through": "2026-05-11"},
    )

    assert day == date(2026, 5, 11)


def test_signal_journal_replay_marks_status_during_batch(monkeypatch):
    class DummyController:
        def __init__(self, *args, **kwargs):
            self.finalized = []

        def start_dynamic(self):
            pass

        def on_external_signal_day(self, day):
            pass

        def refresh_symbols(self, symbols, day):
            pass

        def set_replay_now(self, now):
            pass

        def mark_signal_day(self, day):
            pass

        def finalize(self, final_day=None):
            self.finalized.append(final_day)

    monkeypatch.setattr(
        replay_adapter_module,
        "SimReplayController",
        DummyController,
    )

    class DummySession:
        def __init__(self):
            self.committed = False
            self.rolled_back = False

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

    class DummySignal:
        id = 1
        code = "518880.SH"
        remark = datetime(2026, 5, 8, 9, 30)

    class DummyStrategy:
        strategy_name = "demo"
        gateway = "QMT_SIM"
        poll_interval = 0.01
        is_polling_avtive = True
        _last_signal_orderids = []
        last_signal_id = 0

        def write_log(self, message):
            pass

        def process_signal(self, signal):
            self._last_signal_orderids = ["QMT_SIM.1"]
            return True

        def mark_signal_consumed(self, session, signal, *, status, error_msg):
            self.consumed_status = status

    strategy = DummyStrategy()
    adapter = SignalJournalReplayAdapter(
        strategy,
        gateway=object(),
        idle_settle_seconds=0,
    )
    session = DummySession()

    adapter._process_one_signal(session, DummySignal())

    assert strategy.replay_status == "running"
    assert strategy.consumed_status == "ordered"
    assert session.committed is True
    assert adapter._last_signal_day == date(2026, 5, 8)

    adapter._finalize_after_idle()

    assert strategy.replay_status == "idle"
    assert adapter.controller.finalized == [date(2026, 5, 8)]



def test_live_signal_cutoff_filters_same_day_old_events():
    session = _session()
    for idx, remark in enumerate(
        [
            datetime(2026, 5, 15, 9, 30),
            datetime(2026, 5, 15, 9, 50),
            datetime(2026, 5, 14, 14, 55),
        ],
        start=1,
    ):
        normalized = normalize_trade_signal_payload(
            {
                "signal_uid": f"jq:demo:cutoff:{idx}",
                "code": "518880.SH",
                "pct": "0.1",
                "pct_semantics": PCT_SEMANTICS,
                "type": "BUY_LST",
                "price": "6.5",
                "stg": "demo",
                "remark": remark.strftime("%Y-%m-%d %H:%M:%S"),
            },
            target_stg="demo",
        )
        upsert_trade_signal_event(session, normalized)

    class DummyMainEngine:
        def get_all_accounts(self):
            return []

    class DummySignalEngine:
        main_engine = DummyMainEngine()

    strategy = MySQLSignalStrategyPlus.__new__(MySQLSignalStrategyPlus)
    strategy.signal_engine = DummySignalEngine()
    strategy.strategy_name = "demo"
    strategy.gateway = "QMT"
    strategy.current_dt = datetime(2026, 5, 15, 10, 0)
    strategy.engine_type = EngineType.LIVE.value
    strategy.live_signal_cutoff_dt = datetime(2026, 5, 15, 9, 45)

    rows = strategy.query_trade_signals(session)

    assert [row.remark for row in rows] == [datetime(2026, 5, 15, 9, 50)]


def test_runner_scope_suffix_isolates_shared_source_consumption():
    session = _session()
    normalized = normalize_trade_signal_payload(
        {
            "signal_uid": "jq:shared:1",
            "code": "518880.SH",
            "pct": "0.1",
            "pct_semantics": PCT_SEMANTICS,
            "type": "BUY_LST",
            "price": "6.5",
            "stg": "shared_source",
            "remark": "2026-05-15 09:30:00",
        },
        target_stg="shared_source",
    )
    event, _ = upsert_trade_signal_event(session, normalized)

    class DummyAccount:
        gateway_name = "QMT"
        accountid = "QMT"

    class DummyMainEngine:
        def get_all_accounts(self):
            return [DummyAccount()]

    class DummySignalEngine:
        main_engine = DummyMainEngine()

    def make_strategy(scope_suffix: str):
        strategy = MySQLSignalStrategyPlus.__new__(MySQLSignalStrategyPlus)
        strategy.signal_engine = DummySignalEngine()
        strategy.strategy_name = "visible_strategy"
        strategy.signal_source_stg = "shared_source"
        strategy.signal_application_scope_suffix = scope_suffix
        strategy.gateway = "QMT"
        strategy.current_dt = datetime(2026, 5, 15, 10, 0)
        strategy.engine_type = EngineType.LIVE.value
        strategy.live_signal_cutoff_dt = None
        strategy._last_signal_orderids = []
        return strategy

    local = make_strategy("local")
    cloud = make_strategy("tencent_cloud")

    assert local._application_scope()[0] == "QMT@local"
    assert cloud._application_scope()[0] == "QMT@tencent_cloud"
    assert [row.id for row in local.query_trade_signals(session)] == [event.id]
    local.mark_signal_consumed(session, event, status="skipped")
    session.commit()

    assert local.query_trade_signals(session) == []
    assert [row.id for row in cloud.query_trade_signals(session)] == [event.id]


def test_bound_gateway_wins_over_global_contract_gateway():
    class DummyContract:
        gateway_name = "QMT"

    class DummyMainEngine:
        gateways = {}

        def get_contract(self, vt_symbol):
            return DummyContract()

    class DummySignalEngine:
        main_engine = DummyMainEngine()

    strategy = MySQLSignalStrategyPlus.__new__(MySQLSignalStrategyPlus)
    strategy.signal_engine = DummySignalEngine()
    strategy.gateway = "QMT_SIM_redis_shadow"
    strategy.write_log = lambda message: None

    assert strategy.get_gateway_name("002188.SZSE") == "QMT_SIM_redis_shadow"
