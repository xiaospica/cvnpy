# -*- coding: utf-8 -*-
from vnpy.trader.app import BaseApp

from .engine import APP_NAME, SignalEnginePlus
from .mysql_signal_strategy import MySQLSignalStrategyPlus
from .template import SignalTemplatePlus
from .ui import SignalStrategyWidgetPlus

__all__ = [
    "APP_NAME",
    "SignalEnginePlus",
    "SignalTemplatePlus",
    "MySQLSignalStrategyPlus",
    "SignalStrategyPlusApp",
]


__version__ = "0.0.1"

class SignalStrategyPlusApp(BaseApp):
    """"""

    app_name = APP_NAME
    app_module = __module__
    app_path = __file__
    display_name = "信号策略Plus"
    engine_class = SignalEnginePlus
    widget_name = "SignalStrategyWidgetPlus"
    icon_name = "signal.ico"
