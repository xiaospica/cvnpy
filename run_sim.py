# -*- coding: utf-8 -*-
import os
import sys
from pathlib import Path

os.environ["VNPY_DOCK_BACKEND"] = "ads"

# 让 vnpy_ml_strategy 能 import qlib_strategy_core + Microsoft qlib,
# 不用 pip install -e, submodule 更新后自动生效.
_CORE_DIR = Path(__file__).resolve().parent / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

# qlib 源码由 qlib_strategy_dev 仓库提供 (vnpy Python env 的镜像没有 pyqlib),
# 支持 QLIB_SOURCE_ROOT 环境变量覆写路径
_QLIB_SOURCE = Path(
    os.getenv("QLIB_SOURCE_ROOT", r"F:\Quant\code\qlib_strategy_dev")
)
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists() and str(_QLIB_SOURCE) not in sys.path:
    sys.path.insert(0, str(_QLIB_SOURCE))

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_webtrader import WebTraderApp
from vnpy_qmt import QmtGateway
from vnpy_qmt_sim import QmtSimGateway
from vnpy_xt import XtGateway
from vnpy_signal_strategy import SignalStrategyApp
from vnpy_signal_strategy_plus import SignalStrategyPlusApp
from vnpy_signal_strategy_plus_backtester import SignalBacktesterApp
# from vnpy_ctastrategy import CtaEngine, CtaStrategyApp
# from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_signal_strategy_plus.strategies.multistrategy_signal_strategy import MultiStrategySignalStrategyPlus
from vnpy_tushare_pro import TushareProApp
from vnpy_ml_strategy import MLStrategyApp

def main():
    """"""
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 加载模拟网关
    # main_engine.add_gateway(XtGateway, gateway_name="Xt")
    main_engine.add_gateway(QmtGateway, gateway_name="QMT")
    # main_engine.add_gateway(QmtSimGateway, gateway_name="QMT_SIM")
    
    # 加载信号策略应用
    # main_engine.add_app(SignalStrategyApp)
    main_engine.add_app(SignalStrategyPlusApp)
    main_engine.add_app(SignalBacktesterApp)
    # main_engine.add_app(CtaStrategyApp)
    # main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)
    main_engine.add_app(WebTraderApp)
    
    # 手动添加策略（在UI中也可添加）
    # 策略会自动从外部配置文件加载其对应的配置
    # signal_engine = main_engine.get_engine("SignalStrategy")
    signal_engine_plus = main_engine.get_engine("SignalStrategyPlus")

    main_window = MainWindow(main_engine, event_engine)
    # main_window.showMaximized()
    main_window.resize(2200, 1200)
    main_window.show()

    qapp.exec()

if __name__ == "__main__":
    main()
