"""``MLEngine`` — 注册到 vnpy ``MainEngine`` 的引擎.

职责:
1. 维护 ``DailyTimeTaskScheduler`` 在后台线程跑 ``run_daily_pipeline``
2. 通过 ``QlibPredictor`` 调 subprocess 做推理 (Phase 2.2 实现)
3. ``MetricsCache`` 保留最新 N 日单日指标, 供 webtrader REST 查询 (Phase 2.5)
4. ``publish_metrics`` 原子写 latest.json + 发 EVENT_ML_METRICS (Phase 2.5)

本文件 Phase 2.1 先落骨架, 预留 Phase 2.2-2.5 扩展点.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, Optional

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import BaseEngine, MainEngine

from vnpy_tushare_pro.scheduler import DailyTimeTaskScheduler

from .base import APP_NAME, EVENT_ML_METRICS
from .monitoring.cache import MetricsCache
from .monitoring.publisher import publish_metrics as _publish_metrics
from .predictors.qlib_predictor import QlibPredictor
from .predictors.model_registry import ModelRegistry
from .utils.trade_calendar import make_calendar


class MLEngine(BaseEngine):
    """ML 策略引擎. 继承 BaseEngine 以便 main_engine.add_app 注册."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        super().__init__(main_engine, event_engine, APP_NAME)

        # 调度器 (Phase 2.1)
        self.scheduler = DailyTimeTaskScheduler()

        # 策略注册表 {strategy_name: MLStrategyTemplate}
        self.strategies: Dict[str, Any] = {}

        # MetricsCache (Phase 2.5) — thread-safe 最新值 + 最近 30 日 ring buffer
        self._metrics_cache = MetricsCache(max_history_days=30)

        # Predictor + ModelRegistry + calendar (Phase 2.2, 默认实现)
        self._predictor = QlibPredictor()
        self._model_registry = ModelRegistry()
        self._trade_calendar = None  # init_engine 里 lazy 初始化 (需要 provider_uri)

        # 订单归属表: vt_orderid → strategy_name. 策略发单时调 track_order 登记.
        self._orderid_to_strategy: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # BaseEngine 接口
    # ------------------------------------------------------------------

    def init_engine(self) -> None:
        """vnpy 启动时调用一次."""
        self.scheduler.start()
        self.register_order_listener()

    def close(self) -> None:
        """vnpy 关闭时调用."""
        self.scheduler.stop(wait=True)

    # ------------------------------------------------------------------
    # 策略生命周期 — 被 MLStrategyAdapter 调用 (Phase 2.6)
    # ------------------------------------------------------------------

    def add_strategy(self, strategy_name: str, strategy_obj: Any) -> None:
        self.strategies[strategy_name] = strategy_obj

    def remove_strategy(self, strategy_name: str) -> None:
        self.strategies.pop(strategy_name, None)

    def register_daily_job(
        self,
        strategy_name: str,
        trigger_time: str,
        callback: Callable[[], None],
    ) -> None:
        """委托给 DailyTimeTaskScheduler."""
        self.scheduler.register_daily_job(
            name=strategy_name,
            time_str=trigger_time,
            job_func=callback,
        )

    def unregister_daily_job(self, strategy_name: str) -> None:
        # scheduler 暂无 unregister_daily_job, 用内部 APS remove 接口
        try:
            self.scheduler.scheduler.remove_job(strategy_name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Predictor / calendar 注入 (Phase 2.2)
    # ------------------------------------------------------------------

    def set_predictor(self, predictor) -> None:
        self._predictor = predictor

    def set_trade_calendar(self, calendar) -> None:
        self._trade_calendar = calendar

    def ensure_trade_calendar(self, provider_uri: Optional[str]) -> None:
        """Lazy-init trade calendar using provider_uri from策略 parameter."""
        if self._trade_calendar is None and provider_uri:
            self._trade_calendar = make_calendar(provider_uri)

    def is_trade_day(self, d: date) -> bool:
        if self._trade_calendar is None:
            # 未注入时默认周一至周五
            return d.weekday() < 5
        return self._trade_calendar.is_trade_day(d)

    def run_inference(
        self,
        bundle_dir: str,
        live_end: date,
        lookback_days: int,
        strategy_name: str,
        inference_python: str,
        output_root: str,
        provider_uri: str,
        baseline_path: Optional[str] = None,
        timeout_s: int = 180,
    ) -> Dict[str, Any]:
        """委托给 QlibPredictor (Phase 2.2). 现在是 Phase 2.1 占位."""
        if self._predictor is None:
            raise RuntimeError("Predictor not set (Phase 2.2 will inject)")
        return self._predictor.predict(
            bundle_dir=bundle_dir,
            live_end=live_end,
            lookback_days=lookback_days,
            strategy_name=strategy_name,
            inference_python=inference_python,
            output_root=output_root,
            provider_uri=provider_uri,
            baseline_path=baseline_path,
            timeout_s=timeout_s,
        )

    # ------------------------------------------------------------------
    # Bundle 校验 (Phase 2.2)
    # ------------------------------------------------------------------

    def validate_bundle(self, bundle_dir: str) -> Dict[str, Any]:
        """校验 bundle 目录 + 记入 ModelRegistry. 返回 manifest dict."""
        if not bundle_dir:
            raise ValueError("bundle_dir is empty")
        return self._model_registry.register(bundle_dir)

    def get_manifest(self, bundle_dir: str) -> Optional[Dict[str, Any]]:
        return self._model_registry.get(bundle_dir)

    # ------------------------------------------------------------------
    # Metrics 发布 (Phase 2.5 完整实现)
    # ------------------------------------------------------------------

    def publish_metrics(
        self,
        strategy_name: str,
        metrics: Dict[str, Any],
        trade_date: Optional[date] = None,
        output_root: Optional[str] = None,
        status: str = "ok",
    ) -> None:
        """Phase 2.5 — 更新 MetricsCache + 原子写 latest.json + EVENT_ML_METRICS.

        Parameters
        ----------
        strategy_name : str
        metrics : dict
            From subprocess metrics.json (payload structure defined in core).
        trade_date : date, optional
            Defaults to today. Required for latest.json layout.
        output_root : str, optional
            Strategy output base dir. Required for latest.json write.
            If None, skip file I/O (still update cache + emit event).
        status : str
            "ok" / "empty" / "failed". Failed status won't overwrite latest.json.
        """
        trade_date = trade_date or date.today()

        if output_root:
            _publish_metrics(
                self._metrics_cache,
                strategy_name=strategy_name,
                trade_date=trade_date,
                output_root=output_root,
                metrics=metrics,
                status=status,
            )
        else:
            # Cache-only path (test / engine without output_root wired)
            self._metrics_cache.update(strategy_name, metrics)

        # EVENT_ML_METRICS always emitted from main process EventEngine
        self.put_event(
            EVENT_ML_METRICS + strategy_name,
            {
                "strategy": strategy_name,
                "trade_date": trade_date.isoformat(),
                "status": status,
                "metrics": metrics,
            },
        )

    def get_latest_metrics(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        """供 webtrader adapter 查询 (Phase 2.6)."""
        return self._metrics_cache.get_latest(strategy_name)

    def get_metrics_history(self, strategy_name: str, n: int = 30) -> list:
        return self._metrics_cache.get_history(strategy_name, days=n)

    # ------------------------------------------------------------------
    # 事件工具
    # ------------------------------------------------------------------

    def put_event(self, event_type: str, payload: Any) -> None:
        self.event_engine.put(Event(type=event_type, data=payload))

    # ------------------------------------------------------------------
    # 订单管理 — AutoResubmitMixin 触发重挂时的流转中枢
    # ------------------------------------------------------------------
    #
    # MLStrategyTemplate 不继承 SignalTemplatePlus, 下单走
    # ``main_engine.send_order(req, gateway)`` 直连. 但策略收到 on_order
    # 回报后, 需要把订单回调到对应策略实例处理 (含 AutoResubmitMixin).
    # 我们维护一个 orderid → strategy_name 的映射, 在 MLEngine 注册 EventEngine
    # 的 eOrder 监听, 找到目标策略调 on_order(order).

    def register_order_listener(self) -> None:
        """在 init_engine 里调用一次. 订阅 eOrder / eTrade."""
        from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE

        self.event_engine.register(EVENT_ORDER, self._process_order_event)
        self.event_engine.register(EVENT_TRADE, self._process_trade_event)

    def track_order(self, vt_orderid: str, strategy_name: str) -> None:
        """策略发单后调用, 登记归属关系."""
        self._orderid_to_strategy[vt_orderid] = strategy_name

    def _process_order_event(self, event) -> None:
        from vnpy.trader.object import OrderData

        order: OrderData = event.data
        strategy_name = self._orderid_to_strategy.get(order.vt_orderid)
        if strategy_name is None:
            return
        strategy = self.strategies.get(strategy_name)
        if strategy is None:
            return
        try:
            if hasattr(strategy, "on_order"):
                strategy.on_order(order)
        except Exception as exc:
            print(f"[MLEngine] strategy.on_order failed: {exc}")

    def _process_trade_event(self, event) -> None:
        from vnpy.trader.object import TradeData

        trade: TradeData = event.data
        strategy_name = self._orderid_to_strategy.get(trade.vt_orderid)
        if strategy_name is None:
            return
        strategy = self.strategies.get(strategy_name)
        if strategy is None:
            return
        try:
            if hasattr(strategy, "on_trade"):
                strategy.on_trade(trade)
        except Exception as exc:
            print(f"[MLEngine] strategy.on_trade failed: {exc}")
