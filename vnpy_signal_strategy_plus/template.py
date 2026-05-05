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
        # 订单序号：与 vnpy_ml_strategy.MlTemplate 保持一致，每次 send_order 自增；
        # 用于 get_order_reference 生成 ``{strategy_name}:{seq}`` 格式 reference。
        self._order_seq: int = 0

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
        """生成订单 reference - 与 vnpy_ml_strategy.MlTemplate 完全对齐，
        格式 ``{strategy_name}:{seq}``（重挂时尾部追加 ``R``）。

        关键：mlearnweb 后端 ``list_strategy_trades`` 用
        ``prefix = f"{strategy_name}:"`` 做 ``startswith`` 过滤把成交归到策略
        卡片。早期版本用 ``{APP_NAME}_{strategy_name}``（无冒号无序号）会被
        过滤掉，导致前端 5173 的 TradesCard 永远显示空。

        AutoResubmitMixin 可读 ``self._is_resubmitting`` 判断当前是否处于
        重挂语境，与 MlTemplate 行为一致。
        """
        self._order_seq += 1
        suffix = "R" if getattr(self, "_is_resubmitting", False) else ""
        return f"{self.strategy_name}:{self._order_seq}{suffix}"

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
