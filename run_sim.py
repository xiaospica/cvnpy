# -*- coding: utf-8 -*-
from pathlib import Path

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_qmt_sim import QmtSimGateway
from vnpy_signal_strategy import SignalStrategyApp
from vnpy_signal_strategy.strategies.multistrategy_signal_strategy import MultiStrategySignalStrategy

def main():
    """"""
    qapp = create_qapp()
    qss_path = Path.cwd().joinpath("resources/darkstyle.qss")
    if qss_path.exists():
        extra_qss = qss_path.read_text(encoding="utf-8")
        qapp.setStyleSheet(f"{qapp.styleSheet()}\n{extra_qss}")

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 加载模拟网关
    main_engine.add_gateway(QmtSimGateway)
    
    # 加载信号策略应用
    main_engine.add_app(SignalStrategyApp)
    
    # 手动添加策略（在UI中也可添加）
    # 策略会自动从外部配置文件加载其对应的配置
    signal_engine = main_engine.get_engine("SignalStrategy")
    # if MultiStrategySignalStrategy.strategy_name not in signal_engine.strategies:
    #     signal_engine.add_strategy(MultiStrategySignalStrategy)

    main_window = MainWindow(main_engine, event_engine)
    # main_window.showMaximized()
    main_window.resize(2200, 1200)
    main_window.show()

    qapp.exec()

if __name__ == "__main__":
    main()
