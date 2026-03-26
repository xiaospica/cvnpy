from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.event import EventEngine
from vnpy.trader.object import (
    OrderRequest,
    SubscribeRequest,
    OrderData,
    TradeData,
    TickData,
    BarData
)
from vnpy.trader.utility import load_json, save_json

from .base import APP_NAME
if TYPE_CHECKING:
    from .engine import SignalEnginePlus


class SignalTemplatePlus(ABC):
    """"""

    author: str = ""
    parameters: List[str] = []
    variables: List[str] = []
    strategy_name: str = ""

    def __init__(
        self,
        signal_engine: Any,
    ):
        """"""
        self.signal_engine: "SignalEnginePlus" = signal_engine
        
        if not self.strategy_name:
            self.strategy_name = self.__class__.__name__

        self.inited: bool = False
        self.trading: bool = False

    def update_setting(self, setting: Dict[str, Any]) -> None:
        """
        Update strategy parameter setting.
        """
        d = self.__dict__
        for key in self.parameters:
            if key in setting:
                d[key] = setting[key]
                print(d[key])

    def get_parameters(self) -> Dict[str, Any]:
        """
        Get strategy parameters.
        """
        strategy_parameters = {}
        for name in self.parameters:
            strategy_parameters[name] = getattr(self, name)
        return strategy_parameters

    def get_variables(self) -> Dict[str, Any]:
        """
        Get strategy variables.
        """
        strategy_variables = {}
        for name in self.variables:
            strategy_variables[name] = getattr(self, name)
        return strategy_variables

    def get_data(self) -> Dict[str, Any]:
        """
        Get strategy data.
        """
        strategy_data = {
            "strategy_name": self.strategy_name,
            "vt_symbol": "",  # Signal strategy might not have a single vt_symbol
            "class_name": self.__class__.__name__,
            "author": self.author,
            "parameters": self.get_parameters(),
            "variables": self.get_variables(),
            "inited": self.inited,
            "trading": self.trading
        }
        return strategy_data

    @classmethod
    def get_class_parameters(cls) -> dict:
        """
        Get default parameters dict of strategy class.
        """
        class_parameters: dict = {}
        for name in cls.parameters:
            class_parameters[name] = getattr(cls, name)
        return class_parameters

    def get_order_reference(self) -> str:
        """
        获取委托的 reference 标识。
        默认格式: {APP_NAME}_{strategy_name}
        """
        return f"{APP_NAME}_{self.strategy_name}"

    @abstractmethod
    def on_init(self) -> None:
        """
        Callback when strategy is inited.
        """
        pass

    @abstractmethod
    def on_start(self) -> None:
        """
        Callback when strategy is started.
        """
        pass

    @abstractmethod
    def on_stop(self) -> None:
        """
        Callback when strategy is stopped.
        """
        pass

    @abstractmethod
    def on_timer(self) -> None:
        """
        Callback when timer fired.
        """
        pass

    def on_tick(self, tick: TickData) -> None:
        """
        Callback of new tick data update.
        """
        return

    def on_bar(self, bar: BarData) -> None:
        """
        Callback of new bar data update.
        """
        return

    def on_order(self, order: OrderData) -> None:
        """
        Callback of new order data update.
        """
        pass

    def on_trade(self, trade: TradeData) -> None:
        """
        Callback of new trade data update.
        """
        pass

    def send_order(
        self,
        vt_symbol: str,
        direction: Any,
        offset: Any,
        price: float,
        volume: float,
        order_type: Any = None
    ) -> List[str]:
        """
        Send a new order.
        """
        if self.trading:
            return self.signal_engine.send_order(
                self, vt_symbol, direction, offset, price, volume, order_type
            )
        return []

    def cancel_order(self, vt_orderid: str) -> None:
        """
        Cancel an existing order.
        """
        if self.trading:
            self.signal_engine.cancel_order(self, vt_orderid)

    def write_log(self, msg: str) -> None:
        """
        Write a log message.
        """
        self.signal_engine.write_log(msg, self)

    def put_event(self) -> None:
        """
        Put an event to update strategy status.
        """
        if self.inited:
            self.signal_engine.put_strategy_event(self)

    def get_engine_type(self) -> Any:
        """
        Return engine type.
        """
        return self.signal_engine.engine_type
