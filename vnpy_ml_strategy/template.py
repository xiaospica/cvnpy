"""``MLStrategyTemplate`` — ML 日频策略的抽象基类.

继承 ``SignalTemplatePlus`` 以复用 vnpy 下单管道 (``send_order`` 等). 添加
日频 pipeline 编排 (``run_daily_pipeline``) + 子类必实现的数据/预测接口.

Pipeline 概览(run_daily_pipeline,APS 后台线程触发):

  is_trade_day? ──no──> return (心跳仍发)
  │ yes
  ├─ (主进程,启子进程前)  ← 这里几乎没事, 大头在子进程里
  ├─ subprocess: fetch → preprocess → predict → metrics → 写三件套
  ├─ 主进程读 diagnostics.json:
  │    status=ok  → select_topk → T+1/涨跌停过滤 → generate_orders → send_order
  │    status=empty → EVENT_ML_EMPTY, 不下单
  │    status=failed → EVENT_ML_FAILED, 不下单
  └─ publish_metrics → MetricsCache + EVENT_ML_METRICS

子类只需定义 parameters/variables + 可选覆盖 ``select_topk`` /
``generate_orders``. 数据获取与预测已由 subprocess 封装.
"""

from __future__ import annotations

from abc import ABC
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from vnpy_signal_strategy_plus.template import SignalTemplatePlus

from .base import (
    EVENT_ML_EMPTY,
    EVENT_ML_FAILED,
    EVENT_ML_HEARTBEAT,
    EVENT_ML_METRICS,
    EVENT_ML_PREDICTION,
    InferenceStatus,
    Stage,
)


class MLStrategyTemplate(SignalTemplatePlus, ABC):
    """ML 策略基类 — 日频 pipeline 编排."""

    author = "ml-team"

    # vnpy 参数系统自动注入 UI (子类覆盖)
    parameters: List[str] = [
        # 模型与 bundle
        "bundle_dir",          # 如 D:/vnpy_models/csi300_lgb/ab2711.../
        "inference_python",    # 研究机 Python 3.11 路径
        # 调度
        "trigger_time",        # "09:15"
        # 选股
        "topk",
        "n_drop",
        "cash_per_order",
        # 存盘
        "output_root",         # D:/ml_output 的根 (实际子目录按 {strategy}/{yyyymmdd})
        # 推理参数
        "lookback_days",
        "provider_uri",        # qlib bin 根
        "baseline_path",       # 可选, 默认 bundle_dir/baseline.parquet
        # 其它
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

    # ------------------------------------------------------------------
    # Parameter defaults (子类可在 __init__ 里覆盖)
    # ------------------------------------------------------------------

    bundle_dir: str = ""
    inference_python: str = r"E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe"
    trigger_time: str = "09:15"
    topk: int = 7
    n_drop: int = 1
    cash_per_order: float = 100000.0
    output_root: str = "D:/ml_output"
    lookback_days: int = 60
    provider_uri: str = ""
    baseline_path: str = ""  # 空则用 bundle_dir/baseline.parquet
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

    # ------------------------------------------------------------------
    # 主入口 - 被 DailyTimeTaskScheduler 在后台线程调用
    # ------------------------------------------------------------------

    def run_daily_pipeline(self) -> None:
        """完整日频 pipeline — 主进程编排, 推理在子进程, 下单在主进程."""
        today = date.today()
        self.last_run_date = str(today)
        self.last_error = ""
        self.last_stage = ""

        # 1. 非交易日短路
        if not self._is_trade_day(today):
            self._emit_heartbeat(reason="non_trading_day")
            return

        # 2. 启子进程推理 (Phase 2.2 QlibPredictor.predict)
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

        # 3. 按 status 分支
        if diag.get("status") == InferenceStatus.FAILED.value:
            self._emit_failed(reason=diag.get("error_message", "subprocess failed"))
            return

        if diag.get("status") == InferenceStatus.EMPTY.value:
            self._emit_empty()
            self._publish_metrics(metrics)  # 空预测也要记指标
            return

        # status=ok 继续选股 + 下单
        self.last_stage = Stage.SELECT.value
        selected = self.select_topk(pred_df)

        if self.enable_trading:
            self.last_stage = Stage.ORDER.value
            self.generate_orders(selected)

        self.last_stage = Stage.PUBLISH.value
        self._publish_metrics(metrics)
        self._emit_prediction(selected, metrics)

    # ------------------------------------------------------------------
    # 默认实现 — 子类通常不需要覆盖
    # ------------------------------------------------------------------

    def _is_trade_day(self, d: date) -> bool:
        """非交易日短路. 由 MLEngine 注入的 trade_calendar 模块决定."""
        return self.signal_engine.is_trade_day(d)

    def select_topk(self, pred_df: pd.DataFrame) -> pd.DataFrame:
        """从当日预测里挑 top-K (按 score 降序). 子类可覆盖做加权/分层."""
        if pred_df is None or pred_df.empty:
            return pd.DataFrame()
        last_dt = pred_df.index.get_level_values("datetime").max()
        slice_df = pred_df.xs(last_dt, level="datetime")
        return slice_df.sort_values("score", ascending=False).head(self.topk)

    def generate_orders(self, selected: pd.DataFrame) -> None:
        """把 top-K 转成 OrderRequest 并发出. 默认等权.

        **重点**: A 股 T+1 / 涨跌停过滤在此方法内做, 由 MLEngine 注入的
        ``order_filter`` 决定. 子类可覆盖实现更精细的仓位管理.
        """
        raise NotImplementedError("subclass should implement generate_orders")

    # ------------------------------------------------------------------
    # 事件发送 (主进程 EventEngine)
    # ------------------------------------------------------------------

    def _publish_metrics(self, metrics: Dict[str, Any]) -> None:
        """写 latest.json + emit EVENT_ML_METRICS. 委托 MLEngine 做.

        状态语义 — 与 diagnostics.status 对齐:
        - ok: 有效预测,latest.json 覆盖写
        - empty: 子进程跑完但无预测,latest.json 仍覆盖 (反映最新跑了一次的事实)
        - failed: 子进程失败,latest.json **不覆盖**,保留上次成功的监控数据
        """
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

    # ------------------------------------------------------------------
    # vnpy 标准生命周期
    # ------------------------------------------------------------------

    def on_init(self) -> None:
        """策略初始化 — 注册每日任务, 校验 bundle 完整性."""
        self.signal_engine.register_daily_job(
            strategy_name=self.strategy_name,
            trigger_time=self.trigger_time,
            callback=self.run_daily_pipeline,
        )
        # 校验 bundle_dir 存在 + manifest 版本兼容
        self.signal_engine.validate_bundle(self.bundle_dir)
        self.write_log(f"[{self.strategy_name}] on_init completed")

    def on_start(self) -> None:
        self.write_log(f"[{self.strategy_name}] on_start")

    def on_stop(self) -> None:
        self.signal_engine.unregister_daily_job(strategy_name=self.strategy_name)
        self.write_log(f"[{self.strategy_name}] on_stop")

    def on_timer(self) -> None:
        # 秒级 tick, 只做心跳转发 + 订单状态巡检, 不跑推理 pipeline
        pass
