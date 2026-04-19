"""Web 服务引擎.

- 通过 ``RpcServer`` 向 Web 进程暴露 ``MainEngine`` 的交易方法;
- 通过 ``StrategyEngineAdapter`` 把多种策略引擎统一成一套 RPC 接口;
- 订阅常用事件 + 所有已知策略引擎的状态事件, 转发给 RPC 订阅端。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from vnpy.event import Event, EventEngine
from vnpy.rpc import RpcServer
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.event import (
    EVENT_ACCOUNT,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_POSITION,
    EVENT_TICK,
    EVENT_TRADE,
)

from .strategy_adapter import (
    AddStrategyRequest,
    StrategyEngineAdapter,
    StrategyOpResult,
    build_adapters,
)


APP_NAME = "RpcService"

#: 节点侧启动时间, 用于 /node/health 的 uptime 计算
_START_TIME: float = time.time()


class WebEngine(BaseEngine):
    """Web 服务引擎。"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)

        self.server: RpcServer = RpcServer()
        self.adapters: Dict[str, StrategyEngineAdapter] = {}
        # 节点身份 (聚合层识别用), 可通过 set_node_info 注入
        self.node_id: str = ""
        self.display_name: str = ""

        self.init_server()
        self.register_event()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def init_server(self) -> None:
        """注册 RPC 方法。"""
        # 交易相关 (MainEngine 原生方法)
        self.server.register(self.main_engine.connect)
        self.server.register(self.main_engine.subscribe)
        self.server.register(self.main_engine.send_order)
        self.server.register(self.main_engine.cancel_order)

        self.server.register(self.main_engine.get_contract)
        self.server.register(self.main_engine.get_order)
        self.server.register(self.main_engine.get_all_ticks)
        self.server.register(self.main_engine.get_all_orders)
        self.server.register(self.main_engine.get_all_trades)
        self.server.register(self.main_engine.get_all_positions)
        self.server.register(self.main_engine.get_all_accounts)
        self.server.register(self.main_engine.get_all_contracts)

        # 节点元信息
        self.server.register(self.get_node_info)
        self.server.register(self.get_node_health)

        # 策略管理 (通用适配层)
        self.server.register(self.list_strategy_engines)
        self.server.register(self.list_strategy_classes)
        self.server.register(self.get_strategy_class_params)
        self.server.register(self.list_strategies)
        self.server.register(self.get_strategy)
        self.server.register(self.add_strategy)
        self.server.register(self.init_strategy)
        self.server.register(self.start_strategy)
        self.server.register(self.stop_strategy)
        self.server.register(self.remove_strategy)
        self.server.register(self.edit_strategy)
        self.server.register(self.init_all_strategies)
        self.server.register(self.start_all_strategies)
        self.server.register(self.stop_all_strategies)

        # ML 监控 (MlStrategy 专属)
        self.server.register(self.get_ml_metrics_latest)
        self.server.register(self.get_ml_metrics_history)
        self.server.register(self.get_ml_prediction_summary)
        self.server.register(self.get_ml_health)

    def start_server(self, rep_address: str, pub_address: str) -> None:
        """启动 RPC 服务器。"""
        if self.server.is_active():
            return
        # 在启动时(而不是 __init__)构建适配器, 确保其它 App 的 engine 都已 add_app
        if not self.adapters:
            self.adapters = build_adapters(self.main_engine)
            self._refresh_event_subscription()
        self.server.start(rep_address, pub_address)

    def set_node_info(self, node_id: str, display_name: str = "") -> None:
        """由启动脚本调用, 设置节点身份。"""
        self.node_id = node_id
        self.display_name = display_name or node_id

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------

    def register_event(self) -> None:
        """注册通用事件。策略相关事件在 ``start_server`` 后动态补齐。"""
        for ev in (EVENT_TICK, EVENT_TRADE, EVENT_ORDER, EVENT_POSITION, EVENT_ACCOUNT):
            self.event_engine.register(ev, self.process_generic_event)
        self.event_engine.register(EVENT_LOG, self.process_log_event)

    def _refresh_event_subscription(self) -> None:
        """遍历已构建的 Adapter, 订阅它们声明的状态事件。"""
        for adapter in self.adapters.values():
            if not adapter.event_type:
                continue
            self.event_engine.register(adapter.event_type, self.process_strategy_event)

    def process_generic_event(self, event: Event) -> None:
        """tick/order/trade/position/account -> RPC 发布原始 topic."""
        self.server.publish(event.type, event.data)

    def process_log_event(self, event: Event) -> None:
        """日志事件独立发布, 方便前端按 topic 过滤。"""
        self.server.publish(EVENT_LOG, event.data)

    def process_strategy_event(self, event: Event) -> None:
        """把各引擎的 ``EVENT_*_STRATEGY`` 事件按原 topic 透传。

        路由层会根据 topic 反查 adapter 并在 WS 消息里补上 ``engine`` 字段。
        """
        self.server.publish(event.type, event.data)

    # ------------------------------------------------------------------
    # 节点元信息
    # ------------------------------------------------------------------

    def get_node_info(self) -> Dict[str, Any]:
        gateways: List[Dict[str, Any]] = []
        for name, gw in (self.main_engine.gateways or {}).items():
            gateways.append({
                "name": name,
                "connected": bool(getattr(gw, "connected", False)),
            })
        engines: List[str] = sorted((self.main_engine.engines or {}).keys())
        return {
            "node_id": self.node_id or "unnamed",
            "display_name": self.display_name or self.node_id or "unnamed",
            "started_at": _START_TIME,
            "uptime": time.time() - _START_TIME,
            "gateways": gateways,
            "engines": engines,
            "strategy_engines": [a.describe() for a in self.adapters.values()],
        }

    def get_node_health(self) -> Dict[str, Any]:
        queue_size = 0
        queue = getattr(self.event_engine, "_queue", None)
        if queue is not None:
            try:
                queue_size = queue.qsize()
            except Exception:
                queue_size = -1
        gateways = {
            name: bool(getattr(gw, "connected", False))
            for name, gw in (self.main_engine.gateways or {}).items()
        }
        return {
            "status": "ok",
            "uptime": time.time() - _START_TIME,
            "event_queue_size": queue_size,
            "gateway_status": gateways,
        }

    # ------------------------------------------------------------------
    # 策略管理 (统一入口)
    # ------------------------------------------------------------------

    def _get_adapter(self, engine: str) -> Optional[StrategyEngineAdapter]:
        return self.adapters.get(engine)

    def _err(self, message: str, http_status: int = 400) -> Dict[str, Any]:
        return {"ok": False, "message": message, "data": {"http_status": http_status}}

    def list_strategy_engines(self) -> List[Dict[str, Any]]:
        return [a.describe() for a in self.adapters.values()]

    def list_strategy_classes(self, engine: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        return {"ok": True, "message": "", "data": adapter.list_classes()}

    def get_strategy_class_params(self, engine: str, class_name: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        try:
            params = adapter.get_class_params(class_name)
        except KeyError:
            return self._err(f"策略类不存在: {class_name}", 404)
        return {"ok": True, "message": "", "data": params}

    def list_strategies(self, engine: str = "") -> Dict[str, Any]:
        result: List[Dict[str, Any]] = []
        if engine:
            adapter = self._get_adapter(engine)
            if adapter is None:
                return self._err(f"未注册的策略引擎: {engine}", 404)
            adapters = [adapter]
        else:
            adapters = list(self.adapters.values())
        for ad in adapters:
            for info in ad.list_strategies():
                result.append(info.to_dict())
        return {"ok": True, "message": "", "data": result}

    def get_strategy(self, engine: str, name: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        info = adapter.get_strategy(name)
        if info is None:
            return self._err(f"策略实例不存在: {name}", 404)
        return {"ok": True, "message": "", "data": info.to_dict()}

    def add_strategy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        engine = payload.get("engine", "")
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        if "add" not in adapter.capabilities:
            return self._err("该引擎不支持 add_strategy", 501)
        req = AddStrategyRequest.from_dict(payload)
        return adapter.add_strategy(req).to_dict()

    def _call_op(self, engine: str, op: str, name: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        if op not in adapter.capabilities:
            return self._err(f"该引擎不支持 {op}_strategy", 501)
        func = getattr(adapter, f"{op}_strategy")
        result: StrategyOpResult = func(name)
        return result.to_dict()

    def init_strategy(self, engine: str, name: str) -> Dict[str, Any]:
        return self._call_op(engine, "init", name)

    def start_strategy(self, engine: str, name: str) -> Dict[str, Any]:
        return self._call_op(engine, "start", name)

    def stop_strategy(self, engine: str, name: str) -> Dict[str, Any]:
        return self._call_op(engine, "stop", name)

    def remove_strategy(self, engine: str, name: str) -> Dict[str, Any]:
        return self._call_op(engine, "remove", name)

    def edit_strategy(self, engine: str, name: str, setting: Dict[str, Any]) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        if "edit" not in adapter.capabilities:
            return self._err("该引擎不支持 edit_strategy", 501)
        return adapter.edit_strategy(name, setting or {}).to_dict()

    def init_all_strategies(self, engine: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        return adapter.init_all().to_dict()

    def start_all_strategies(self, engine: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        return adapter.start_all().to_dict()

    def stop_all_strategies(self, engine: str) -> Dict[str, Any]:
        adapter = self._get_adapter(engine)
        if adapter is None:
            return self._err(f"未注册的策略引擎: {engine}", 404)
        return adapter.stop_all().to_dict()

    # ------------------------------------------------------------------
    # ML 监控专属 (MlStrategy 引擎)
    # ------------------------------------------------------------------

    ML_ENGINE_NAME = "MlStrategy"

    def _ml_adapter(self):
        return self._get_adapter(self.ML_ENGINE_NAME)

    def get_ml_metrics_latest(self, name: str) -> Dict[str, Any]:
        adapter = self._ml_adapter()
        if adapter is None:
            return self._err("MlStrategy 引擎未注册", 404)
        metrics = adapter.get_latest_metrics(name)
        if metrics is None:
            return self._err(f"策略无最新指标: {name}", 404)
        return {"ok": True, "message": "", "data": metrics}

    def get_ml_metrics_history(self, name: str, days: int = 30) -> Dict[str, Any]:
        adapter = self._ml_adapter()
        if adapter is None:
            return self._err("MlStrategy 引擎未注册", 404)
        return {"ok": True, "message": "", "data": adapter.get_metrics_history(name, days=days)}

    def get_ml_prediction_summary(self, name: str) -> Dict[str, Any]:
        adapter = self._ml_adapter()
        if adapter is None:
            return self._err("MlStrategy 引擎未注册", 404)
        summary = adapter.get_prediction_summary(name)
        if summary is None:
            return self._err(f"策略无最新预测: {name}", 404)
        return {"ok": True, "message": "", "data": summary}

    def get_ml_health(self) -> Dict[str, Any]:
        adapter = self._ml_adapter()
        if adapter is None:
            return self._err("MlStrategy 引擎未注册", 404)
        return {"ok": True, "message": "", "data": adapter.get_health()}

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.server.stop()
        self.server.join()
