"""
Defines constants and objects used in CtaStrategy App.
"""

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta

from vnpy.trader.constant import Direction, Offset, Interval
from vnpy.trader.utility import round_to, ZoneInfo
from .locale import _

APP_NAME = "SignalStrategyPlus"

CHINA_TZ = ZoneInfo("Asia/Shanghai")

class EngineType(Enum):
    LIVE = _("实盘")
    BACKTESTING = _("回测")


class BacktestingMode(Enum):
    BAR = 1
    TICK = 2


EVENT_SignalStrategy_LOG = "eSignalStrategyLog"
EVENT_SignalStrategy_STRATEGY = "eSignalStrategyPlus"


INTERVAL_DELTA_MAP: dict[Interval, timedelta] = {
    Interval.TICK: timedelta(milliseconds=1),
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}
