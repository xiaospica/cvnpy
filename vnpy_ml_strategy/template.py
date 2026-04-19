"""``MLStrategyTemplate`` — ML 日频策略的抽象基类.

Phase B 重构: 不再继承 SignalTemplatePlus (跨 app 耦合). 组合方式:
    MLStrategyTemplate(AutoResubmitMixin, ABC)

  - AutoResubmitMixin 来自 vnpy_order_utils, 提供撤单/拒单自动重挂能力
  - 本类提供:
      * send_order / cancel_order — 直接调 main_engine, 不经过 SignalEnginePlus
      * on_order / on_trade — 调 AutoResubmitMixin.on_order_for_resubmit (mixin 契约)
      * on_timer — 调 on_timer_for_resubmit
      * run_daily_pipeline — 日频主流程 (subprocess 推理 + T+1 过滤 + 下单)
      * update_setting / get_parameters / get_variables — 设置系统 (模仿 SignalTemplatePlus)
      * write_log / get_order_reference — 基础工具

Host 契约 (OrderResubmitHost 要求):
  * ``gateway: str``       — parameters 字段
  * ``signal_engine``      — 指向 MLEngine; 它的 ``.main_engine`` 是 vnpy MainEngine
                              (mixin 查 gateway + tick 走这条路径)
  * ``send_order(...)``    — 本类实现
  * ``write_log(msg)``     — 本类实现

Pipeline 概览(run_daily_pipeline, APS 后台线程触发):

  is_trade_day? ──no──> return (心跳仍发)
  │ yes
  ├─ subprocess: fetch → preprocess → predict → metrics → 写三件套
  ├─ 主进程读 diagnostics.json:
  │    status=ok  → select_topk → T+1/涨跌停过滤 → generate_orders → send_order
  │    status=empty → EVENT_ML_EMPTY, 不下单
  │    status=failed → EVENT_ML_FAILED, 不下单
  └─ publish_metrics → MetricsCache + EVENT_ML_METRICS
"""

from __future__ import annotations

from abc import ABC
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderData, OrderRequest, TradeData

from vnpy_order_utils import AutoResubmitMixin

from .base import (
    EVENT_ML_EMPTY,
    EVENT_ML_FAILED,
    EVENT_ML_HEARTBEAT,
    EVENT_ML_METRICS,
    EVENT_ML_PREDICTION,
    InferenceStatus,
    Stage,
)


class MLStrategyTemplate(AutoResubmitMixin, ABC):
    """ML 策略基类 — 日频 pipeline 编排 + 订单重挂能力.

    注意 MRO: AutoResubmitMixin 必须在左, 它的 ``__init__(*args, **kwargs)``
    会先初始化 _resubmit_count 等状态, 再通过 super().__init__ 链到 ABC.
    """

    author = "ml-team"

    # vnpy 参数系统 (子类覆盖; 注意 gateway 是 AutoResubmitMixin 宿主契约必需项)
    parameters: List[str] = [
        "bundle_dir",          # 如 D:/vnpy_models/csi300_lgb/ab2711.../
        "inference_python",    # 研究机 Python 3.11 路径
        "trigger_time",        # "09:15"
        "topk",
        "n_drop",
        "cash_per_order",
        "gateway",             # 如 "QMT" / "QMT_SIM"; 下单 + mixin 价格查询都用
        "output_root",         # D:/ml_output 根
        "lookback_days",
        "provider_uri",        # qlib bin 根
        "baseline_path",       # 空则用 bundle_dir/baseline.parquet
        "monitor_window_days",
        "enable_trading",      # 干跑开关
        "subprocess_timeout_s",
    ]
    variables: List[str] = [
        "last_run_date",
        "last_stage",
        "last_error",
        "last_n_pred",
        "last_ic",
        "last_psi_mean",
        "last_status",
        "last_duration_ms",
        "last_model_run_id",
    ]

    # Parameter defaults
    bundle_dir: str = ""
    inference_python: str = r"E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe"
    trigger_time: str = "09:15"
    topk: int = 7
    n_drop: int = 1
    cash_per_order: float = 100000.0
    gateway: str = ""
    output_root: str = "D:/ml_output"
    lookback_days: int = 60
    provider_uri: str = ""
    baseline_path: str = ""
    monitor_window_days: int = 30
    enable_trading: bool = False
    subprocess_timeout_s: int = 180

    # Runtime state
    last_run_date: str = ""
    last_stage: str = ""
    last_error: str = ""
    last_n_pred: int = 0
    last_ic: float = float("nan")
    last_psi_mean: float = float("nan")
    last_status: str = ""
    last_duration_ms: int = 0
    last_model_run_id: str = ""

    # vnpy 生命周期 flag (模仿 SignalTemplatePlus 的约定)
    inited: bool = False
    trading: bool = False

    # -----------------------------------------------------------------
    # __init__ — mixin 需要 (signal_engine, strategy_name) 位置参数, ABC 不吃参
    # -----------------------------------------------------------------

    def __init__(self, signal_engine: Any, strategy_name: str = "") -> None:
        """
        Parameters
        ----------
        signal_engine : MLEngine
            为了符合 AutoResubmitMixin 的宿主契约 (它通过
            ``self.signal_engine.main_engine`` 拿 MainEngine 查 tick/gateway),
            属性名沿用 ``signal_engine``. 对 ML app 语义上就是 MLEngine.
        strategy_name : str
            策略实例名. 作为 orderid 回调的索引键, 也作为 webtrader adapter
            查询指标的键.
        """
        self.signal_engine = signal_engine
        self.strategy_name = strategy_name
        self._order_seq = 0
        # 调 mixin 的 __init__ 初始化 _resubmit_count 等
        super().__init__()

    # -----------------------------------------------------------------
    # 设置系统 (模仿 SignalTemplatePlus 对外形态)
    # -----------------------------------------------------------------

    def update_setting(self, setting: Dict[str, Any]) -> None:
        for name in self.parameters:
            if name in setting:
                setattr(self, name, setting[name])

    def get_parameters(self) -> Dict[str, Any]:
        return {name: getattr(self, name, None) for name in self.parameters}

    def get_variables(self) -> Dict[str, Any]:
        return {name: getattr(self, name, None) for name in self.variables}

    # -----------------------------------------------------------------
    # 下单管道 (Option F: 直连 main_engine, 不经过 SignalEnginePlus)
    # -----------------------------------------------------------------

    def get_order_reference(self) -> str:
        """生成订单 reference — 用于 mixin 重挂时打标,以及成交归因.

        AutoResubmitMixin 可读取 self._is_resubmitting 判断当前是否在重挂语境.
        """
        self._order_seq += 1
        suffix = "R" if getattr(self, "_is_resubmitting", False) else ""
        return f"{self.strategy_name}:{self._order_seq}{suffix}"

    def send_order(
        self,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        order_type: Optional[OrderType] = None,
    ) -> List[str]:
        """通过 MainEngine.send_order 发单, 登记归属到 MLEngine 的 orderid 表.

        Mixin 的 _process_single_resubmit_task 也调本方法 (走同一契约).
        """
        if not self.trading and not getattr(self, "_is_resubmitting", False):
            # 非交易状态下只允许 mixin 内部重挂, 外部调用一律静默拒绝
            self.write_log(f"[{self.strategy_name}] trading=False, 拒绝外部 send_order")
            return []

        try:
            symbol, exchange_str = vt_symbol.rsplit(".", 1)
            exchange = Exchange(exchange_str)
        except (ValueError, KeyError):
            self.write_log(f"[{self.strategy_name}] 合约代码格式错误 {vt_symbol}")
            return []

        if not self.gateway:
            self.write_log(f"[{self.strategy_name}] gateway 未配置, 无法下单")
            return []

        req = OrderRequest(
            symbol=symbol,
            exchange=exchange,
            direction=direction,
            offset=offset,
            type=order_type or OrderType.LIMIT,
            price=float(price),
            volume=float(volume),
            reference=self.get_order_reference(),
        )
        main_engine = self._main_engine()
        vt_orderid = main_engine.send_order(req, self.gateway)
        if not vt_orderid:
            return []

        # 登记归属, 让 MLEngine 收到 eOrder / eTrade 时能找回策略实例
        self.signal_engine.track_order(vt_orderid, self.strategy_name)
        return [vt_orderid]

    def cancel_order(self, vt_orderid: str) -> None:
        from vnpy.trader.object import CancelRequest

        main_engine = self._main_engine()
        # vnpy 的 cancel_order 需要一个 CancelRequest
        order = main_engine.get_order(vt_orderid)
        if order is None:
            self.write_log(f"[{self.strategy_name}] 撤单失败: 订单不存在 {vt_orderid}")
            return
        req = CancelRequest(orderid=order.orderid, symbol=order.symbol, exchange=order.exchange)
        main_engine.cancel_order(req, order.gateway_name)

    def _main_engine(self):
        """拿 vnpy MainEngine — mixin 的 adjust_resubmit_price 也走这条路径."""
        main_engine = getattr(self.signal_engine, "main_engine", None)
        if main_engine is None:
            raise RuntimeError(
                "MLStrategyTemplate 无法拿到 main_engine. signal_engine 需是 MLEngine 实例."
            )
        return main_engine

    # -----------------------------------------------------------------
    # vnpy 事件回调 — 委托给 mixin + 子类扩展点
    # -----------------------------------------------------------------

    def on_order(self, order: OrderData) -> None:
        """MLEngine 路由 eOrder 到这里. 委托给 mixin 做重挂判断."""
        try:
            self.on_order_for_resubmit(order)
        except Exception as exc:
            self.write_log(f"[{self.strategy_name}] on_order_for_resubmit 异常: {exc}")

    def on_trade(self, trade: TradeData) -> None:
        """MLEngine 路由 eTrade 到这里. 默认空实现, 子类可覆盖做持仓更新."""
        pass

    def on_timer(self) -> None:
        """MLEngine 的秒级 timer 路由到这里 (若注册了). 委托给 mixin 做重挂队列扫描."""
        try:
            self.on_timer_for_resubmit()
        except Exception as exc:
            self.write_log(f"[{self.strategy_name}] on_timer_for_resubmit 异常: {exc}")

    # -----------------------------------------------------------------
    # write_log — mixin 宿主契约必需项
    # -----------------------------------------------------------------

    def write_log(self, msg: str) -> None:
        """轻量日志, 直接打印 + 可选发 EVENT_ML_LOG."""
        prefix = f"[{self.strategy_name}] " if self.strategy_name else "[MLStrategy] "
        print(prefix + msg)

    # -----------------------------------------------------------------
    # 主入口 - 被 DailyTimeTaskScheduler 在后台线程调用
    # -----------------------------------------------------------------

    def run_daily_pipeline(self) -> None:
        """完整日频 pipeline — 主进程编排, 推理在子进程, 下单在主进程."""
        today = date.today()
        self.last_run_date = str(today)
        self.last_error = ""
        self.last_stage = ""

        if not self._is_trade_day(today):
            self._emit_heartbeat(reason="non_trading_day")
            return

        self.last_stage = Stage.PREDICT.value
        try:
            result = self.signal_engine.run_inference(
                bundle_dir=self.bundle_dir,
                live_end=today,
                lookback_days=self.lookback_days,
                strategy_name=self.strategy_name,
                inference_python=self.inference_python,
                output_root=self.output_root,
                provider_uri=self.provider_uri,
                baseline_path=self.baseline_path or None,
                timeout_s=self.subprocess_timeout_s,
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_status = InferenceStatus.FAILED.value
            self._emit_failed(reason=self.last_error)
            return

        diag = result["diagnostics"]
        metrics = result.get("metrics", {})
        pred_df = result.get("pred_df")

        self.last_status = diag.get("status", "")
        self.last_duration_ms = diag.get("duration_ms", 0)
        self.last_model_run_id = diag.get("model_run_id", "")
        self.last_n_pred = diag.get("rows", 0)
        self.last_ic = metrics.get("ic", float("nan"))
        self.last_psi_mean = metrics.get("psi_mean", float("nan"))

        if diag.get("status") == InferenceStatus.FAILED.value:
            self._emit_failed(reason=diag.get("error_message", "subprocess failed"))
            return

        if diag.get("status") == InferenceStatus.EMPTY.value:
            self._emit_empty()
            self._publish_metrics(metrics)
            return

        self.last_stage = Stage.SELECT.value
        selected = self.select_topk(pred_df)

        # Persist selections regardless of enable_trading — downstream UIs (Tab1
        # "最新 TopK 信号" / Tab2 "历史回溯") depend on having the per-day
        # selections.parquet on disk even in dry-run mode.
        self.last_stage = Stage.SAVE.value
        try:
            self.persist_selections(selected)
        except Exception as exc:
            self.write_log(f"persist_selections failed: {type(exc).__name__}: {exc}")

        if self.enable_trading:
            self.last_stage = Stage.ORDER.value
            self.generate_orders(selected)

        self.last_stage = Stage.PUBLISH.value
        self._publish_metrics(metrics)
        self._emit_prediction(selected, metrics)

    # -----------------------------------------------------------------
    # 默认实现 + 子类扩展点
    # -----------------------------------------------------------------

    def _is_trade_day(self, d: date) -> bool:
        return self.signal_engine.is_trade_day(d)

    def select_topk(self, pred_df: pd.DataFrame) -> pd.DataFrame:
        if pred_df is None or pred_df.empty:
            return pd.DataFrame()
        last_dt = pred_df.index.get_level_values("datetime").max()
        slice_df = pred_df.xs(last_dt, level="datetime")
        return slice_df.sort_values("score", ascending=False).head(self.topk)

    def persist_selections(self, selected: pd.DataFrame) -> None:
        """Write selections.parquet to {output_root}/{name}/{yyyymmdd}/.

        Default impl writes canonical schema (trade_date, instrument, rank,
        score, weight, target_price, side, model_run_id). Subclasses may
        override to customize (e.g. add sector weights, risk budgeting).

        Always runs — does not depend on ``enable_trading``.
        """
        from .persistence.result_store import ResultStore
        from .persistence.schema import (
            COL_INSTRUMENT, COL_MODEL_RUN_ID, COL_RANK, COL_SIDE,
            COL_TARGET_PRICE, COL_TRADE_DATE, COL_WEIGHT,
        )
        if selected is None or selected.empty:
            return
        today = date.today()
        sel_df = selected.reset_index().copy()
        sel_df[COL_TRADE_DATE] = today.strftime("%Y-%m-%d")
        sel_df[COL_INSTRUMENT] = sel_df.get("instrument", sel_df.iloc[:, 0])
        sel_df[COL_RANK] = range(1, len(sel_df) + 1)
        sel_df[COL_WEIGHT] = 1.0 / len(sel_df)
        sel_df[COL_TARGET_PRICE] = float("nan")
        sel_df[COL_SIDE] = "long"
        sel_df[COL_MODEL_RUN_ID] = self.last_model_run_id
        store = ResultStore(self.output_root)
        store.write_selections(self.strategy_name, today, sel_df)

    def generate_orders(self, selected: pd.DataFrame) -> None:
        """子类实现. 内部调 self.send_order(...). 仅在 enable_trading=True 时被调用."""
        raise NotImplementedError("subclass should implement generate_orders")

    # -----------------------------------------------------------------
    # 事件发送
    # -----------------------------------------------------------------

    def _publish_metrics(self, metrics: Dict[str, Any]) -> None:
        self.signal_engine.publish_metrics(
            strategy_name=self.strategy_name,
            metrics=metrics,
            trade_date=date.today(),
            output_root=self.output_root,
            status=self.last_status or "ok",
        )

    def _emit_prediction(self, selected: pd.DataFrame, metrics: Dict[str, Any]) -> None:
        payload = {
            "strategy": self.strategy_name,
            "trade_date": self.last_run_date,
            "topk": int(self.topk),
            "model_run_id": self.last_model_run_id,
            "holdings": [
                {"instrument": idx, "score": float(row["score"])}
                for idx, row in selected.iterrows()
            ] if selected is not None and not selected.empty else [],
        }
        self.signal_engine.put_event(EVENT_ML_PREDICTION + self.strategy_name, payload)

    def _emit_failed(self, reason: str) -> None:
        self.signal_engine.put_event(
            EVENT_ML_FAILED + self.strategy_name,
            {"strategy": self.strategy_name, "reason": reason, "stage": self.last_stage},
        )

    def _emit_empty(self) -> None:
        self.signal_engine.put_event(
            EVENT_ML_EMPTY + self.strategy_name,
            {"strategy": self.strategy_name, "trade_date": self.last_run_date},
        )

    def _emit_heartbeat(self, reason: str = "") -> None:
        self.signal_engine.put_event(
            EVENT_ML_HEARTBEAT + self.strategy_name,
            {"strategy": self.strategy_name, "reason": reason},
        )

    # -----------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------

    def on_init(self) -> None:
        """策略初始化 — 注册每日任务, 校验 bundle 完整性."""
        self.signal_engine.register_daily_job(
            strategy_name=self.strategy_name,
            trigger_time=self.trigger_time,
            callback=self.run_daily_pipeline,
        )
        self.signal_engine.validate_bundle(self.bundle_dir)
        self.inited = True
        self.write_log("on_init completed")

    def on_start(self) -> None:
        self.trading = True
        self.write_log("on_start")

    def on_stop(self) -> None:
        self.trading = False
        self.signal_engine.unregister_daily_job(strategy_name=self.strategy_name)
        self.write_log("on_stop")
