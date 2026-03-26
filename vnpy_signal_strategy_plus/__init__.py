# -*- coding: utf-8 -*-
from pathlib import Path
from vnpy.trader.app import BaseApp

from .base import APP_NAME
from .engine import SignalEnginePlus
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
    from .locale import _

    app_name: str = APP_NAME
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = _("Signal策略plus")
    engine_class: type[SignalEnginePlus] = SignalEnginePlus
    widget_name: str = "SignalStrategyWidgetPlus"
    icon_name: str = str(app_path.joinpath("ui", "signal_strategy.ico"))
