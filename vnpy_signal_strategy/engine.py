from typing import Any, Dict, List, Optional, Type, Union
from datetime import datetime
from collections import defaultdict
import importlib
import traceback
import json
from pathlib import Path

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.object import (
    OrderRequest,
    SubscribeRequest,
    LogData,
    OrderData,
    TradeData,
    TickData,
    ContractData,
    PositionData
)
from vnpy.trader.event import (
    EVENT_TICK,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_POSITION,
    EVENT_TIMER,
    EVENT_LOG,
)
from vnpy.trader.constant import (
    Direction,
    Offset,
    OrderType,
    Interval,
    Status,
    Exchange
)
from vnpy.trader.utility import load_json, save_json, round_to
from vnpy.trader.converter import OffsetConverter

from vnpy_signal_strategy.template import SignalTemplate
from vnpy_signal_strategy.mysql_signal_strategy import MySQLSignalStrategy

APP_NAME = "SignalStrategy"

class SignalEngine(BaseEngine):
    """"""

    engine_name = "SignalStrategy"

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        """"""
        super().__init__(main_engine, event_engine, self.engine_name)

        self.strategies: Dict[str, SignalTemplate] = {}
        self.symbol_strategy_map: defaultdict = defaultdict(list)
        self.orderid_strategy_map: Dict[str, SignalTemplate] = {}

        self.classes: Dict[str, Type[SignalTemplate]] = {}

        self.offset_converter: OffsetConverter = OffsetConverter(self.main_engine)

    def init_engine(self) -> None:
        """
        """
        self.load_strategy_class()
        self.register_event()
        self.write_log("信号策略引擎初始化成功")

    def init_all_strategies(self) -> None:
        """
        Init all strategies.
        """
        for strategy_name in list(self.strategies.keys()):
            self.init_strategy(strategy_name)

    def start_all_strategies(self) -> None:
        """
        Start all strategies.
        """
        for strategy_name in list(self.strategies.keys()):
            self.start_strategy(strategy_name)

    def stop_all_strategies(self) -> None:
        """
        Stop all strategies.
        """
        for strategy_name in list(self.strategies.keys()):
            self.stop_strategy(strategy_name)

    def load_strategy_class(self) -> None:
        """
        Load strategy class from source code.
        """
        # Load internal strategies

        path = Path(__file__).parent
        strategies_path = path.joinpath("strategies")
        if not strategies_path.exists():
            return

        for filename in strategies_path.iterdir():
            if filename.suffix == ".py":
                self.load_strategy_file(str(filename))

    def load_strategy_file(self, filepath: str) -> None:
        """
        Load strategy class from source code file.
        """
        try:
            spec = importlib.util.spec_from_file_location("strategy_module", filepath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            for name in dir(module):
                value = getattr(module, name)
                if (
                    isinstance(value, type)
                    and issubclass(value, SignalTemplate)
                    and value not in [SignalTemplate, MySQLSignalStrategy]
                ):
                    print(value.__name__)
                    self.classes[value.__name__] = value
        except Exception:
            msg = f"策略文件加载失败：{filepath}，\n{traceback.format_exc()}"
            self.write_log(msg)

    def register_event(self) -> None:
        """"""
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_tick_event(self, event: Event) -> None:
        """"""
        tick: TickData = event.data
        strategies = self.symbol_strategy_map[tick.vt_symbol]
        if not strategies:
            return

        for strategy in strategies:
            if strategy.inited:
                self.call_strategy_func(strategy, strategy.on_tick, tick)

    def process_order_event(self, event: Event) -> None:
        """"""
        order: OrderData = event.data
        self.offset_converter.update_order(order)
        
        strategy = self.orderid_strategy_map.get(order.vt_orderid)
        if not strategy:
            return

        self.call_strategy_func(strategy, strategy.on_order, order)

    def process_trade_event(self, event: Event) -> None:
        """"""
        trade: TradeData = event.data
        self.offset_converter.update_trade(trade)

        strategy = self.orderid_strategy_map.get(trade.vt_orderid)
        if not strategy:
            return

        self.call_strategy_func(strategy, strategy.on_trade, trade)

    def process_timer_event(self, event: Event) -> None:
        """"""
        for strategy in self.strategies.values():
            if strategy.inited:
                self.call_strategy_func(strategy, strategy.on_timer)

    def call_strategy_func(
        self, strategy: SignalTemplate, func: callable, params: Any = None
    ) -> None:
        """
        Call function of a strategy and catch any exception raised.
        """
        try:
            if params:
                func(params)
            else:
                func()
        except Exception:
            strategy.trading = False
            strategy.inited = False
            
            msg = f"触发异常已停止\n{traceback.format_exc()}"
            self.write_log(msg, strategy)

    def add_strategy(
        self,
        strategy_class: Union[str, Type[SignalTemplate]]
    ) -> None:
        """
        Add a new strategy.
        """
        if isinstance(strategy_class, str):
            if strategy_class not in self.classes:
                self.write_log(f"创建策略失败，找不到策略类{strategy_class}")
                return
            strategy_class = self.classes[strategy_class]

        strategy = strategy_class(self)
        if strategy.strategy_name in self.strategies:
            self.write_log(f"创建策略失败，存在重名{strategy.strategy_name}")
            return

        self.strategies[strategy.strategy_name] = strategy
        
        self.put_strategy_event(strategy)
        self.write_log(f"创建策略成功{strategy.strategy_name}")

    def get_all_strategy_class_names(self) -> list:
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str) -> dict:
        strategy_class = self.classes[class_name]
        parameters = {}
        for name in strategy_class.parameters:
            parameters[name] = getattr(strategy_class, name)
        return parameters
    
    def get_strategy_parameters(self, strategy_name: str) -> dict:
        strategy = self.strategies[strategy_name]
        return strategy.get_parameters()

    def init_strategy(self, strategy_name: str) -> None:
        """
        Init a strategy.
        """
        strategy = self.strategies[strategy_name]
        if strategy.inited:
            self.write_log(f"策略已初始化{strategy_name}")
            return

        self.call_strategy_func(strategy, strategy.on_init)
        strategy.inited = True
        self.put_strategy_event(strategy)
        self.write_log(f"策略初始化完成{strategy_name}")

    def start_strategy(self, strategy_name: str) -> None:
        """
        Start a strategy.
        """
        strategy = self.strategies[strategy_name]
        if not strategy.inited:
            self.write_log(f"策略未初始化{strategy_name}")
            return

        if strategy.trading:
            self.write_log(f"策略已启动{strategy_name}")
            return

        self.call_strategy_func(strategy, strategy.on_start)
        strategy.trading = True
        self.put_strategy_event(strategy)
        self.write_log(f"策略启动完成{strategy_name}")

    def stop_strategy(self, strategy_name: str) -> None:
        """
        Stop a strategy.
        """
        strategy = self.strategies[strategy_name]
        if not strategy.trading:
            return

        self.call_strategy_func(strategy, strategy.on_stop)
        strategy.trading = False
        self.put_strategy_event(strategy)
        self.write_log(f"策略停止完成{strategy_name}")

    def remove_strategy(self, strategy_name: str) -> None:
        """
        Remove a strategy.
        """
        strategy = self.strategies[strategy_name]
        if strategy.trading:
            self.stop_strategy(strategy_name)

        self.strategies.pop(strategy_name)
        
        self.put_strategy_event(strategy)
        self.write_log(f"策略移除完成{strategy_name}")

    def send_order(
        self,
        strategy: SignalTemplate,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        order_type: OrderType
    ) -> List[str]:
        """
        Send a new order.
        """
        gateway_name = vt_symbol.split(".")[1]
        
        # Create order request
        req = OrderRequest(
            symbol=vt_symbol.split(".")[0],
            exchange=Exchange(gateway_name), # This might be wrong if gateway name != exchange name, usually vt_symbol = symbol.exchange
            direction=direction,
            offset=offset,
            type=order_type or OrderType.LIMIT,
            price=price,
            volume=volume,
            reference=f"{APP_NAME}_{strategy.strategy_name}"
        )
        
        # Correct exchange parsing
        # vt_symbol format: symbol.exchange (e.g. 600000.SSE)
        try:
            symbol, exchange_str = vt_symbol.rsplit(".", 1)
            req.symbol = symbol
            req.exchange = Exchange(exchange_str)
        except Exception:
            self.write_log(f"合约代码格式错误{vt_symbol}", strategy)
            return []

        # Find gateway
        contract = self.main_engine.get_contract(vt_symbol)
        if contract:
            gateway_name = contract.gateway_name
        else:
            # Fallback for simulation or if contract not found
            # Try to find a gateway that supports this exchange
            for gateway in self.main_engine.gateways.values():
                 if req.exchange in gateway.exchanges:
                     gateway_name = gateway.gateway_name
                     break
            else:
                self.write_log(f"未找到合约信息{vt_symbol}，且无支持该交易所的网关，无法下单", strategy)
                return []

        vt_orderid = self.main_engine.send_order(req, gateway_name)
        if not vt_orderid:
            return []

        self.orderid_strategy_map[vt_orderid] = strategy
        return [vt_orderid]

    def cancel_order(self, strategy: SignalTemplate, vt_orderid: str) -> None:
        """
        """
        order = self.main_engine.get_order(vt_orderid)
        if not order:
            return
            
        req = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

    def write_log(self, msg: str, strategy: SignalTemplate = None) -> None:
        """
        Create and put log event.
        """
        if strategy:
            msg = f"[{strategy.strategy_name}] {msg}"
        
        log = LogData(msg=msg, gateway_name=APP_NAME)
        event = Event(EVENT_LOG, log)
        self.event_engine.put(event)

    def put_strategy_event(self, strategy: SignalTemplate) -> None:
        """
        Put an event to update strategy status.
        """
        data = strategy.get_data()
        event = Event("EVENT_SIGNAL_STRATEGY", data)
        self.event_engine.put(event)
