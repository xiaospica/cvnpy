from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
