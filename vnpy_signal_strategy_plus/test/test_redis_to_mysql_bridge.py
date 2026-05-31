from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from vnpy_signal_strategy_plus.scripts import redis_to_mysql_bridge as bridge_mod


def test_bridge_config_defaults_mysql_timeouts(tmp_path: Path) -> None:
    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "redis": {
                    "host": "127.0.0.1",
                    "port": 6379,
                },
                "mysql": {
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "root",
                    "password": "secret",
                    "db": "mysql",
                },
                "subscriptions": [
                    {
                        "stream_key": "demo",
                        "target_stg": "demo",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg = bridge_mod.BridgeConfig.from_file(config_path)

    assert cfg.mysql.connect_timeout == 10
    assert cfg.mysql.read_timeout == 30
    assert cfg.mysql.write_timeout == 30
    assert cfg.mysql.lock_wait_timeout == 10
    assert cfg.message_timeout_s == 30.0


def test_mysql_writer_uses_driver_and_lock_wait_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}
    engine = object()

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return engine

    def fake_listens_for(target, name):
        assert target is engine
        assert name == "connect"

        def _decorator(func):
            captured["connect_listener"] = func
            return func

        return _decorator

    def fake_create_all(target):
        captured["create_all_engine"] = target

    monkeypatch.setattr(bridge_mod, "create_engine", fake_create_engine)
    monkeypatch.setattr(bridge_mod.event, "listens_for", fake_listens_for)
    monkeypatch.setattr(
        bridge_mod.SignalJournalBase.metadata,
        "create_all",
        fake_create_all,
    )

    bridge_mod.MySQLWriter(
        bridge_mod.MySQLCfg(
            host="db.local",
            port=3306,
            user="user",
            password="p#ss",
            db="mysql",
            connect_timeout=3,
            read_timeout=4,
            write_timeout=5,
            lock_wait_timeout=6,
        )
    )

    assert captured["kwargs"]["connect_args"] == {
        "connect_timeout": 3,
        "read_timeout": 4,
        "write_timeout": 5,
    }
    assert captured["create_all_engine"] is engine

    statements: list[str] = []

    class FakeCursor:
        def execute(self, statement: str) -> None:
            statements.append(statement)

        def close(self) -> None:
            statements.append("closed")

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    captured["connect_listener"](FakeConnection(), None)

    assert statements == [
        "SET SESSION innodb_lock_wait_timeout=6",
        "SET SESSION lock_wait_timeout=6",
        "closed",
    ]


def test_stream_consumer_fatal_exits_on_handler_timeout(caplog) -> None:
    exit_codes: list[int] = []

    def stuck_handler(_sub, _payload, _redis_id) -> bool:
        time.sleep(0.05)
        return True

    def fake_fatal_exit(code: int) -> None:
        exit_codes.append(code)
        raise RuntimeError("fatal exit")

    consumer = bridge_mod.StreamConsumer(
        sub=bridge_mod.Subscription("demo", "demo"),
        redis_client=object(),
        cfg=bridge_mod.RedisCfg(host="127.0.0.1", port=6379),
        on_message=stuck_handler,
        logger=logging.getLogger("test_bridge_timeout"),
        stop_event=threading.Event(),
        message_timeout_s=0.01,
        fatal_exit=fake_fatal_exit,
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(RuntimeError):
        consumer._call_on_message({"code": "000001.SZ"}, "1-0")

    consumer._handler_executor.shutdown(wait=True)

    assert exit_codes == [2]
    assert "handler timeout id=1-0" in caplog.text


def test_stream_consumer_logs_handler_elapsed(caplog) -> None:
    def handler(_sub, _payload, _redis_id) -> bool:
        return True

    consumer = bridge_mod.StreamConsumer(
        sub=bridge_mod.Subscription("demo", "demo"),
        redis_client=object(),
        cfg=bridge_mod.RedisCfg(host="127.0.0.1", port=6379),
        on_message=handler,
        logger=logging.getLogger("test_bridge_elapsed"),
        stop_event=threading.Event(),
        message_timeout_s=1.0,
        fatal_exit=lambda code: None,
    )

    with caplog.at_level(logging.INFO):
        assert consumer._call_on_message({"code": "000001.SZ"}, "1-0") is True

    consumer._handler_executor.shutdown(wait=True)

    assert "handled id=1-0 ok=True elapsed=" in caplog.text
