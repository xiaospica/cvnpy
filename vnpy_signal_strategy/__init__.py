# -*- coding: utf-8 -*-
from vnpy_signal_strategy.engine import SignalEngine
from vnpy_signal_strategy.template import SignalTemplate
from vnpy_signal_strategy.ui import SignalStrategyWidget
from vnpy.trader.app import BaseApp
from vnpy.trader.engine import MainEngine
from vnpy.event import EventEngine
from vnpy_signal_strategy.mysql_signal_strategy import MySQLSignalStrategy

__all__ = [
    # "APP_NAME",
    "SignalEngine",
    "SignalTemplate",
    "MySQLSignalStrategy",
]


__version__ = "0.0.1"

class SignalStrategyApp(BaseApp):
    """"""

    app_name = "SignalStrategy"
    app_module = __module__
    app_path = __file__
    display_name = "信号策略"
    engine_class = SignalEngine
    widget_name = "SignalStrategyWidget"
    icon_name = "signal.ico"
