"""vnpy_ml_strategy — ML 日频策略 app.

使用方式:

    from vnpy.trader.engine import MainEngine
    from vnpy_ml_strategy import MLStrategyApp
    main_engine.add_app(MLStrategyApp)
"""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .base import APP_NAME, Stage, InferenceStatus, EVENT_ML_METRICS, EVENT_ML_PREDICTION
from .engine import MLEngine
from .template import MLStrategyTemplate

__all__ = [
    "APP_NAME",
    "Stage",
    "InferenceStatus",
    "EVENT_ML_METRICS",
    "EVENT_ML_PREDICTION",
    "MLEngine",
    "MLStrategyTemplate",
    "MLStrategyApp",
]

__version__ = "0.1.0"


class MLStrategyApp(BaseApp):
    """机器学习日频策略应用."""

    app_name: str = APP_NAME
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = "ML策略"
    engine_class: type[MLEngine] = MLEngine
    widget_name: str = ""
    icon_name: str = ""
