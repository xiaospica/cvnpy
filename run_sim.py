# -*- coding: utf-8 -*-
from pathlib import Path
import os
os.environ['VNPY_DOCK_BACKEND'] = 'ads'
from PySide6.QtCore import Qt

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_qmt import QmtGateway
from vnpy_qmt_sim import QmtSimGateway
from vnpy_signal_strategy import SignalStrategyApp
from vnpy_signal_strategy_plus import SignalStrategyPlusApp
from vnpy_signal_strategy_plus_backtester import SignalBacktesterApp
from vnpy_ctastrategy import CtaEngine, CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_signal_strategy_plus.strategies.multistrategy_signal_strategy import MultiStrategySignalStrategyPlus
from vnpy_tushare_pro import TushareProApp

def main():
    """"""
    qapp = create_qapp()
    
    is_dark = qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
    if is_dark:
        qss_path = Path.cwd().joinpath("resources/darkstyle.qss")
    else:
        qss_path = Path.cwd().joinpath("resources/lightstyle.qss")
    qapp.setStyleSheet(f"{qapp.styleSheet()}\n{qss_path.read_text(encoding="utf-8")}")

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 加载模拟网关
    # main_engine.add_gateway(QmtGateway, gateway_name="QMT")
    main_engine.add_gateway(QmtSimGateway, gateway_name="QMT_SIM")
    
    # 加载信号策略应用
    main_engine.add_app(SignalStrategyApp)
    main_engine.add_app(SignalStrategyPlusApp)
    main_engine.add_app(SignalBacktesterApp)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(TushareProApp)
    
    # 手动添加策略（在UI中也可添加）
    # 策略会自动从外部配置文件加载其对应的配置
    signal_engine = main_engine.get_engine("SignalStrategy")
    signal_engine_plus = main_engine.get_engine("SignalStrategyPlus")

    main_window = MainWindow(main_engine, event_engine)
    # main_window.showMaximized()
    main_window.resize(2200, 1200)
    main_window.show()

    qapp.exec()

if __name__ == "__main__":
    main()
