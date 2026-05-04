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

from vnpy_common.scheduler import DailyTimeTaskScheduler

from .base import APP_NAME, EVENT_ML_METRICS, EVENT_ML_STRATEGY
from .monitoring.cache import MetricsCache
from .monitoring.publisher import publish_metrics as _publish_metrics
from .predictors.qlib_predictor import QlibPredictor
from .predictors.model_registry import ModelRegistry
from .services.ic_backfill import IcBackfillService, IcBackfillResult
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

        # IC 回填 service (方案 §2.4.5) — 每只策略一个实例, 在 publish_metrics
        # 后台线程触发, 不阻塞主线程. lazy 创建 (需要策略实例上的路径参数).
        self._ic_backfill_services: Dict[str, IcBackfillService] = {}

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

    def run_pipeline_now(self, strategy_name: str, as_of_date=None) -> bool:
        """UI 手动触发一次日频 pipeline (= 模拟 trigger_time 21:00 cron 触发).

        双 cron 架构: 此方法触发 ``{strategy_name}_predict`` job, 即推理 + persist
        selections.parquet, 不下单. 下单由 ``run_open_rebalance_now`` 在 buy_sell_time
        cron 触发.

        ``scheduler.run_job_now`` 同步阻塞直到 job 完成 (subprocess 推理 ~60-90s).
        run_daily_pipeline 内部已用 try/finally 保证发 put_strategy_event.

        Phase 4 回放支持
        ----------------
        ``as_of_date`` (datetime.date) 给定时透传给 ``run_daily_pipeline``，
        让推理子进程命令行 ``--live-end`` 用历史日期，回放控制器逐日调用即可。
        默认 None → 走 ``date.today()``，保持实盘 trigger_time 行为不变。
        """
        if strategy_name not in self.strategies:
            return False
        try:
            kwargs = {}
            if as_of_date is not None:
                kwargs["as_of_date"] = as_of_date
            self.scheduler.run_job_now(strategy_name + "_predict", **kwargs)
            self.put_strategy_event(self.strategies[strategy_name])
            return True
        except Exception as exc:
            print(f"[MLEngine] run_pipeline_now({strategy_name}) failed: {exc}")
            return False

    def run_open_rebalance_now(self, strategy_name: str, as_of_date=None) -> bool:
        """UI / smoke 手动触发一次 09:26 rebalance (= 模拟 buy_sell_time cron 触发).

        触发 ``{strategy_name}_rebalance`` job → run_open_rebalance(as_of_date):
        读 prev_day 的 predictions.parquet + 刷 today 开盘价 → rebalance + send_order.

        smoke fast-forward 时按日序调用模拟实盘 09:26 cron 行为, 与 batch replay 的
        Day=T iteration 语义等价 (都用 prev_day_pred + Day T 撮合).
        """
        if strategy_name not in self.strategies:
            return False
        try:
            kwargs = {}
            if as_of_date is not None:
                kwargs["as_of_date"] = as_of_date
            self.scheduler.run_job_now(strategy_name + "_rebalance", **kwargs)
            self.put_strategy_event(self.strategies[strategy_name])
            return True
        except Exception as exc:
            print(f"[MLEngine] run_open_rebalance_now({strategy_name}) failed: {exc}")
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

    def register_predict_job(
        self,
        strategy_name: str,
        trigger_time: str,
        callback: Callable[[], None],
    ) -> None:
        """注册 trigger_time cron (默认 21:00) → 推理 + persist selections.parquet.

        job id = ``{strategy_name}_predict``. ``run_pipeline_now`` 走此 job.
        """
        self.scheduler.register_daily_job(
            name=strategy_name + "_predict",
            time_str=trigger_time,
            job_func=callback,
        )

    def register_rebalance_job(
        self,
        strategy_name: str,
        buy_sell_time: str,
        callback: Callable[[], None],
    ) -> None:
        """注册 buy_sell_time cron (默认 09:26) → rebalance + send_order.

        job id = ``{strategy_name}_rebalance``. ``run_open_rebalance_now`` 走此 job.
        实盘 best practice: 09:26 集合竞价开盘价已可读, 此时下单 09:30 撮合概率最高.
        """
        self.scheduler.register_daily_job(
            name=strategy_name + "_rebalance",
            time_str=buy_sell_time,
            job_func=callback,
        )

    # 兼容旧 API: 保留 register_daily_job (默认作为 predict job 注册)
    # 新代码请用 register_predict_job / register_rebalance_job
    def register_daily_job(
        self,
        strategy_name: str,
        trigger_time: str,
        callback: Callable[[], None],
    ) -> None:
        """[Deprecated] 旧 API. 新代码用 register_predict_job + register_rebalance_job."""
        self.register_predict_job(strategy_name, trigger_time, callback)

    def unregister_daily_job(self, strategy_name: str) -> None:
        """注销 cron job. 双 cron 架构下需各自调一次:
            unregister_daily_job(name + "_predict")
            unregister_daily_job(name + "_rebalance")
        """
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
        """委托给 QlibPredictor. 按 bundle 自带的 filter_config.filter_id 派生
        snapshot 路径 ``{QS_DATA_ROOT}/snapshots/filtered/{filter_id}_{YYYYMMDD}.parquet``.

        ``filter_parquet_path`` 若 None:
          1. 必须设 env ``QS_DATA_ROOT``, 否则 raise.
          2. 从 ModelRegistry 拿 bundle 的 filter_id (强制), 拼路径.
          3. 快照不存在 → strict raise (引导调用方先跑 daily_ingest 或检查
             ``set_filter_chain_specs`` 是否注入了对应 filter_id).

        显式传入的 ``filter_parquet_path`` (非 None) 优先, 跳过派生.
        """
        import os as _os
        from pathlib import Path as _Path

        if self._predictor is None:
            raise RuntimeError("Predictor not set")

        # 按 bundle 的 filter_id 派生 snapshot 路径; 缺失即 raise
        if filter_parquet_path is None:
            qs_data_root = _os.getenv("QS_DATA_ROOT")
            if not qs_data_root:
                raise RuntimeError(
                    "QS_DATA_ROOT env 未设, 无法定位 filter snapshot. "
                    "实盘启动前必须设此 env (run_ml_headless.py 已 setdefault)"
                )
            filter_id = self._resolve_filter_id(bundle_dir)
            candidate = (
                _Path(qs_data_root) / "snapshots" / "filtered"
                / f"{filter_id}_{live_end.strftime('%Y%m%d')}.parquet"
            )
            if not candidate.exists():
                raise FileNotFoundError(
                    f"filter snapshot 不存在: {candidate} "
                    f"(filter_id={filter_id}, live_end={live_end}). "
                    "排查: (1) daily_ingest 当日是否跑过? "
                    "(2) DailyIngestPipeline.filter_chain_specs 是否含此 filter_id "
                    "(run_ml_headless.py 启动期 set_filter_chain_specs 注入)?"
                )
            filter_parquet_path = str(candidate)

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

    def run_inference_range(
        self,
        bundle_dir: str,
        range_start: date,
        range_end: date,
        lookback_days: int,
        strategy_name: str,
        inference_python: str,
        output_root: str,
        provider_uri: str,
        baseline_path: Optional[str] = None,
        filter_parquet_path: Optional[str] = None,
        timeout_s: int = 3600,
    ) -> Dict[str, Any]:
        """Phase 4 加速回放：批量推理一次产出多日 predictions + diagnostics。

        预期加速 ~10-20x：spawn 一个子进程而非每日一个，省掉 N 次 qlib 加载。
        子进程按 ``{output_root}/{strategy_name}/{yyyymmdd}/`` 写每日子目录。

        Note: 批量模式不写 metrics.json（PSI/KS/IC 等留单日实时模式做）；
        每日 diagnostics.json 含 ``batch_mode=true`` 标记。

        按 bundle 的 filter_id 派生 snapshot 模式 ``{QS_DATA_ROOT}/snapshots/
        filtered/{filter_id}_*.parquet``, 选**最新**(按文件名日期 max). 由于
        ``_stage_filter`` 按 [T-lookback, T] window 回填, 最新 snapshot 总是含完整
        lookback window 的合规数据, 一份足以驱动整个 [range_start, range_end] 回放.

        ``filter_parquet_path`` 若 None:
          1. 必须设 env ``QS_DATA_ROOT``, 否则 raise.
          2. 没有任何 ``{filter_id}_*.parquet`` 匹配 → raise (回放无法用训练时
             固化的 filter, 因为它的日期范围只覆盖训练截止日, 走到训练截止日之后
             handler 全 status=empty 是隐藏的失败).

        显式传入的 ``filter_parquet_path`` (非 None) 优先, 跳过派生.
        """
        import os as _os
        import re as _re
        from pathlib import Path as _Path

        if self._predictor is None:
            raise RuntimeError("Predictor not set")

        # 按 bundle 的 filter_id 派生 snapshot 模式 + 选最新; 缺失即 raise
        if filter_parquet_path is None:
            qs_data_root = _os.getenv("QS_DATA_ROOT")
            if not qs_data_root:
                raise RuntimeError(
                    "QS_DATA_ROOT env 未设, 无法定位 filter snapshot."
                )
            filter_id = self._resolve_filter_id(bundle_dir)
            filter_dir = _Path(qs_data_root) / "snapshots" / "filtered"
            # filter_id 可能含数字 (如 "min_90_days"), 文件名结构 = {filter_id}_{8位日期}.parquet
            # 用 re.escape + \d{8} 严格匹配本 filter_id 的 snapshot
            pattern = _re.compile(rf"^{_re.escape(filter_id)}_(\d{{8}})\.parquet$")
            latest_path = None
            latest_date_str = None
            if filter_dir.exists():
                for entry in filter_dir.iterdir():
                    m = pattern.match(entry.name)
                    if not m:
                        continue
                    d_str = m.group(1)
                    if latest_date_str is None or d_str > latest_date_str:
                        latest_date_str = d_str
                        latest_path = entry

            if latest_path is None:
                raise FileNotFoundError(
                    f"{filter_dir} 无 {filter_id}_*.parquet 快照. 排查: "
                    "(1) daily_ingest 是否跑过? "
                    "(2) DailyIngestPipeline.filter_chain_specs 是否含此 filter_id?"
                )
            filter_parquet_path = str(latest_path)
            from loguru import logger
            logger.info(
                f"[MLEngine] batch 推理使用 filter 快照 {latest_path.name} "
                f"(选最新; range=[{range_start},{range_end}])"
            )

        return self._predictor.run_range(
            bundle_dir=bundle_dir,
            range_start=range_start,
            range_end=range_end,
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
        """校验 bundle 目录 + 记入 ModelRegistry. 返回 manifest dict.

        Phase 2: ModelRegistry.register 还会强制读 filter_config.json + 校验
        filter_id ↔ filter_chain 一致; 失败 raise (老 bundle 缺 filter_config.json
        提示 backfill_filter_config.py).
        """
        if not bundle_dir:
            raise ValueError("bundle_dir is empty")
        return self._model_registry.register(bundle_dir)

    def _resolve_filter_id(self, bundle_dir: str) -> str:
        """从 ModelRegistry 缓存拿 filter_id; 缺失 raise.

        run_inference / run_inference_range 用本方法派生 snapshot 路径.
        """
        cfg = self._model_registry.get_filter_config(bundle_dir)
        if not cfg:
            raise RuntimeError(
                f"bundle {bundle_dir} 未注册到 ModelRegistry 或缺 filter_config; "
                "策略 on_init 应已调 validate_bundle, 检查启动顺序"
            )
        return cfg["filter_id"]

    def list_active_filter_configs(self) -> Dict[str, Dict[str, Any]]:
        """收集本 engine 当前所有策略 bundle 的 filter_config, 按 filter_id 去重.

        Phase 2 task 14: DailyIngestPipeline 启动期用本方法拿到所有需要产 snapshot
        的 filter_chain 集合. 同 filter_id 的多策略合并 (共享一份 snapshot, 节约 ingest).

        Returns
        -------
        Dict[filter_id, filter_config_dict]
            每个 entry 等同 ``ModelRegistry.get_filter_config`` 返回的 dict 内容.
        """
        out: Dict[str, Dict[str, Any]] = {}
        for strategy_name, strat in self.strategies.items():
            bundle_dir = getattr(strat, "bundle_dir", "") or ""
            if not bundle_dir:
                continue
            cfg = self._model_registry.get_filter_config(bundle_dir)
            if not cfg:
                continue
            fid = cfg.get("filter_id")
            if not fid:
                continue
            # 同 filter_id 多策略 → 只留一份 (后注册的覆盖, 一致性已在 register 校过)
            out[fid] = cfg
        return out

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

        # 方案 §2.4.5 — 当日推理产出后顺手触发一次 IC 回填扫描. 后台线程跑,
        # debounce 60s. 失败仅 log, 不影响主流程.
        if status == "ok" and output_root:
            self._trigger_ic_backfill(strategy_name, output_root)

    def _trigger_ic_backfill(self, strategy_name: str, output_root: str) -> None:
        """从策略实例读 provider_uri / inference_python, lazy 起 service 后异步触发."""
        strat = self.strategies.get(strategy_name)
        if strat is None:
            return
        provider_uri = getattr(strat, "provider_uri", None)
        inference_python = getattr(strat, "inference_python", None)
        if not provider_uri or not inference_python:
            return  # 策略没配齐, 跳过
        svc = self._ic_backfill_services.get(strategy_name)
        if svc is None:
            svc = IcBackfillService(
                strategy_name=strategy_name,
                output_root=output_root,
                provider_uri=provider_uri,
                inference_python=inference_python,
                forward_window=int(getattr(strat, "ic_forward_window", 2)),
                scan_days=int(getattr(strat, "ic_backfill_scan_days", 30)),
            )
            self._ic_backfill_services[strategy_name] = svc
        svc.run_async()

    def run_ic_backfill_now(
        self, strategy_name: str, *, scan_days: Optional[int] = None,
    ) -> Optional[IcBackfillResult]:
        """手动触发 IC 回填 (绕过 debounce, 同步阻塞返回结果). 给 REST/手动调用用."""
        strat = self.strategies.get(strategy_name)
        if strat is None:
            return None
        output_root = getattr(strat, "output_root", None)
        provider_uri = getattr(strat, "provider_uri", None)
        inference_python = getattr(strat, "inference_python", None)
        if not (output_root and provider_uri and inference_python):
            return None
        svc = IcBackfillService(
            strategy_name=strategy_name,
            output_root=output_root,
            provider_uri=provider_uri,
            inference_python=inference_python,
            forward_window=int(getattr(strat, "ic_forward_window", 2)),
            scan_days=int(scan_days if scan_days is not None else getattr(strat, "ic_backfill_scan_days", 30)),
        )
        return svc.run_sync()

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
