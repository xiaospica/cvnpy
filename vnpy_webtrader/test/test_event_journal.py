from __future__ import annotations

from vnpy_webtrader.event_journal import append_event, list_events


def test_event_journal_append_and_list(tmp_path):
    db_path = tmp_path / "event_journal.db"
    seq = append_event(
        topic="log",
        node_id="local",
        engine="SignalStrategyPlus",
        data={"strategy_name": "etf_rotation_basic", "msg": "hello"},
        event_ts=1.23,
        db_path=db_path,
    )

    assert seq == 1
    rows = list_events(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["topic"] == "log"
    assert rows[0]["node_id"] == "local"
    assert rows[0]["engine"] == "SignalStrategyPlus"
    assert rows[0]["strategy_name"] == "etf_rotation_basic"
    assert rows[0]["data"]["msg"] == "hello"


def test_event_journal_filters_by_seq_topic_and_strategy(tmp_path):
    db_path = tmp_path / "event_journal.db"
    append_event(topic="log", node_id="local", data={"strategy_name": "s1"}, db_path=db_path)
    append_event(topic="order", node_id="local", data={"strategy_name": "s1"}, db_path=db_path)
    append_event(topic="log", node_id="local", data={"strategy_name": "s2"}, db_path=db_path)

    assert [row["seq"] for row in list_events(since_seq=1, db_path=db_path)] == [2, 3]
    assert [row["topic"] for row in list_events(topic="log", db_path=db_path)] == ["log", "log"]
    rows = list_events(topic="log", strategy_name="s2", db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["strategy_name"] == "s2"
