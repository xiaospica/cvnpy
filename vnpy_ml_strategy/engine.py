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

from .base import APP_NAME, EVENT_ML_METRICS, EVENT_ML_STRATEGY
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

        # 策略类注册表 {class_name: Type} — UI 从下拉框选择类时用
        self.strategy_classes: Dict[str, type] = {}

        # MetricsCache (Phase 2.5) — thread-safe 最新值 + 最近 30 日 ring buffer
        self._metrics_cache = MetricsCache(max_history_days=30)

        # Predictor + ModelRegistry + calendar (Phase 2.2, 默认实现)
        self._predictor = QlibPredictor()
        self._model_registry = ModelRegistry()
        self._trade_calendar = None  # init_engine 里 lazy 初始化 (需要 provider_uri)

        # 订单归属表: vt_orderid → strategy_name. 策略发单时调 track_order 登记.
        self._orderid_to_strategy: Dict[str, str] = {}

        # 幂等 guard — CLI (run_ml_headless) / UI widget 都会调 init_engine;
        # 二次调用会重复 event_engine.register 导致 order 事件被处理两次.
        self._initialized: bool = False

        # Phase 4 — 最近一次 DailyIngestPipeline 状态 (由 _on_ingest_failed 更新).
        # pipeline 可在 run_daily_pipeline 里读此 flag, 若 failed 则发警告事件.
        self._last_ingest_status: Dict[str, Any] = {"status": "unknown"}

    # ------------------------------------------------------------------
    # BaseEngine 接口
    # ------------------------------------------------------------------

    def init_engine(self) -> None:
        """vnpy 启动时调用一次. 幂等 — 第二次调用是 no-op."""
        if self._initialized:
            return
        self.scheduler.start()
        self.register_order_listener()
        self._autoload_strategy_classes()
        # Phase 4 — 监听 DailyIngestPipeline 失败事件, 21:00 推理前可读此 flag.
        try:
            from vnpy_tushare_pro.engine import EVENT_DAILY_INGEST_FAILED
            self.event_engine.register(EVENT_DAILY_INGEST_FAILED, self._on_ingest_failed)
        except ImportError:
            pass  # TushareProApp 未加载时 skip
        self._initialized = True

    def _on_ingest_failed(self, event: Event) -> None:
        """记录最近一次 DailyIngestPipeline 失败 — run_daily_pipeline 可读此 flag 发警告事件."""
        payload = event.data or {}
        self._last_ingest_status = {"status": "failed", "payload": payload}
        from loguru import logger
        logger.warning(f"[MLEngine] DailyIngestPipeline 失败: {payload}")

    def _autoload_strategy_classes(self) -> None:
        """自动登记本 app 内置策略类 — UI combobox 才有东西可选.

        延迟到 init_engine 而不是 __init__ 的原因: 避免 import 循环 (strategies 里
        会 import MLStrategyTemplate, template import base).
        """
        try:
            from .strategies.qlib_ml_strategy import QlibMLStrategy
            self.register_strategy_class(QlibMLStrategy)
        except Exception as exc:  # pragma: no cover — defensive
            print(f"[MLEngine] strategy class autoload failed: {exc}")

    def close(self) -> None:
        """vnpy 关闭时调用."""
        self.scheduler.stop(wait=True)

    # ------------------------------------------------------------------
    # 策略生命周期 — 被 MLStrategyAdapter 调用 (Phase 2.6)
    # ------------------------------------------------------------------

    def register_strategy_class(self, cls: type) -> None:
        """登记策略类到 class 注册表, UI 添加策略时从这里挑类."""
        self.strategy_classes[cls.__name__] = cls

    def get_all_strategy_class_names(self) -> list:
        return list(self.strategy_classes.keys())

    def get_strategy_class_parameters(self, class_name: str) -> Dict[str, Any]:
        """返回某类的默认参数字典, UI 添加策略弹窗用."""
        cls = self.strategy_classes.get(class_name)
        if cls is None:
            return {}
        params = {}
        for name in getattr(cls, "parameters", []):
            params[name] = getattr(cls, name, None)
        return params

    def add_strategy(
        self,
        first,
        second=None,
        setting: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """两种调用形态:

        Form A (UI / 从 class 注册表实例化):
            ``add_strategy(class_name: str, strategy_name: str, setting: dict)``
            从 ``strategy_classes[class_name]`` 拿类, 构造实例, 应用 setting.

        Form B (手动 / 脚本 — 已构造好实例):
            ``add_strategy(strategy_obj)`` 或 ``add_strategy(strategy_obj, strategy_name)``
            直接登记, 从 obj.strategy_name 取 name (或用 second 覆写).

        返回已登记的 strategy 实例.
        """
        # Form B: 第一个参数是实例
        if not isinstance(first, str):
            inst = first
            name = second if isinstance(second, str) and second else getattr(inst, "strategy_name", "")
            if not name:
                raise ValueError("strategy instance 缺少 strategy_name")
            self.strategies[name] = inst
            self.put_strategy_event(inst)
            return inst

        # Form A: (class_name, strategy_name, setting)
        class_name = first
        strategy_name = second if isinstance(second, str) else ""
        if not strategy_name:
            strategy_name = class_name  # fallback: 实例名 = 类名
        cls = self.strategy_classes.get(class_name)
        if cls is None:
            raise ValueError(
                f"strategy class 未注册: {class_name}, "
                f"已知: {list(self.strategy_classes.keys())}"
            )
        inst = cls(self, strategy_name)
        if setting:
            inst.update_setting(setting)
        self.strategies[strategy_name] = inst
        self.put_strategy_event(inst)
        return inst

    def remove_strategy(self, strategy_name: str) -> bool:
        """移除策略. 若还在 trading, 拒绝."""
        strat = self.strategies.get(strategy_name)
        if strat is None:
            return False
        if getattr(strat, "trading", False):
            return False
        self.strategies.pop(strategy_name, None)
        self.put_strategy_event(strat)
        return True

    def init_strategy(self, strategy_name: str) -> bool:
        strat = self.strategies.get(strategy_name)
        if strat is None or getattr(strat, "inited", False):
            return False
        try:
            strat.on_init()
        except Exception as exc:
            print(f"[MLEngine] init_strategy({strategy_name}) failed: {exc}")
            return False
        self.put_strategy_event(strat)
        return True

    def start_strategy(self, strategy_name: str) -> bool:
        strat = self.strategies.get(strategy_name)
        if strat is None or not getattr(strat, "inited", False) or getattr(strat, "trading", False):
            return False
        try:
            strat.on_start()
        except Exception as exc:
            print(f"[MLEngine] start_strategy({strategy_name}) failed: {exc}")
            return False
        self.put_strategy_event(strat)
        return True

    def stop_strategy(self, strategy_name: str) -> bool:
        strat = self.strategies.get(strategy_name)
        if strat is None or not getattr(strat, "trading", False):
            return False
        try:
            strat.on_stop()
        except Exception as exc:
            print(f"[MLEngine] stop_strategy({strategy_name}) failed: {exc}")
            return False
        self.put_strategy_event(strat)
        return True

    def init_all_strategies(self) -> bool:
        ok = True
        for name in list(self.strategies.keys()):
            ok = self.init_strategy(name) and ok
        return ok

    def start_all_strategies(self) -> None:
        for name in list(self.strategies.keys()):
            self.start_strategy(name)

    def stop_all_strategies(self) -> None:
        for name in list(self.strategies.keys()):
            self.stop_strategy(name)

    def run_pipeline_now(self, strategy_name: str) -> bool:
        """UI 手动触发一次日频 pipeline (立即在 APS 后台线程跑)."""
        if strategy_name not in self.strategies:
            return False
        try:
            self.scheduler.run_job_now(strategy_name)
            return True
        except Exception as exc:
            print(f"[MLEngine] run_pipeline_now({strategy_name}) failed: {exc}")
            return False

    def put_strategy_event(self, strategy) -> None:
        """发 EVENT_ML_STRATEGY, UI 消费更新面板."""
        if strategy is None:
            return
        payload = {
            "strategy_name": getattr(strategy, "strategy_name", ""),
            "class_name": strategy.__class__.__name__,
            "inited": bool(getattr(strategy, "inited", False)),
            "trading": bool(getattr(strategy, "trading", False)),
            "parameters": strategy.get_parameters() if hasattr(strategy, "get_parameters") else {},
            "variables": strategy.get_variables() if hasattr(strategy, "get_variables") else {},
            "gateway": getattr(strategy, "gateway", ""),
        }
        self.put_event(EVENT_ML_STRATEGY, payload)

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
        filter_parquet_path: Optional[str] = None,
        timeout_s: int = 180,
    ) -> Dict[str, Any]:
        """委托给 QlibPredictor. Phase 4 v2 支持 filter_parquet_path 透传.

        Phase 4 v2: ``filter_parquet_path`` 若 None, 会尝试从 env ``QS_DATA_ROOT``
        自动拼 ``{QS_DATA_ROOT}/snapshots/filtered/csi300_filtered_{YYYYMMDD}.parquet``,
        保证实盘推理按 live_end 定位到冻结的当日过滤快照(金融实盘可复现硬要求).
        显式传入的 filter_parquet_path 优先.
        """
        import os as _os
        from pathlib import Path as _Path

        if self._predictor is None:
            raise RuntimeError("Predictor not set")

        # Phase 4 v2: 自动按 live_end 拼 filter 快照路径
        if filter_parquet_path is None:
            qs_data_root = _os.getenv("QS_DATA_ROOT")
            if qs_data_root:
                candidate = (
                    _Path(qs_data_root) / "snapshots" / "filtered"
                    / f"csi300_filtered_{live_end.strftime('%Y%m%d')}.parquet"
                )
                if candidate.exists():
                    filter_parquet_path = str(candidate)
                else:
                    # 快照不存在: warn 但不阻塞 (可能是首次跑 / 快照未到), 用 task.json 默认
                    from loguru import logger
                    logger.warning(
                        f"[MLEngine] filter snapshot 不存在 {candidate}, "
                        "handler 用 bundle task.json 默认(可能是训练时点历史值)"
                    )

        return self._predictor.predict(
            bundle_dir=bundle_dir,
            live_end=live_end,
            lookback_days=lookback_days,
            strategy_name=strategy_name,
            inference_python=inference_python,
            output_root=output_root,
            provider_uri=provider_uri,
            baseline_path=baseline_path,
            filter_parquet_path=filter_parquet_path,
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
