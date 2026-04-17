from vnpy.event.engine import Event
from vnpy.trader.object import LogData
from vnpy.trader.engine import LogEngine


def test_log_engine_does_not_format_braces() -> None:
    engine = LogEngine.__new__(LogEngine)
    engine.active = True
    engine.level_map = LogEngine.level_map

    log = LogData(gateway_name="TEST", msg="sql params: {'code': '600000.SH'}")
    event = Event(type="eLog", data=log)

    engine.process_log_event(event)

