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
from datetime import date, datetime, timedelta
import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.object import LogData, OrderData, OrderRequest, TradeData

from vnpy_order_utils import AutoResubmitMixin

from .base import (
    APP_NAME,
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
        "trigger_time",        # 推荐 "21:00" (配合 TushareProApp 20:00 拉数);
                               # 若用 "09:15", live_end 默认 today 会用昨日 bar,
                               # 需自行错位或等待 20:00 拉完数据再触发
        "buy_sell_time",       # 实盘开盘交易时间，默认 "09:30"。T 日 trigger_time 推理产出
                               # topk 后，T+1 日 buy_sell_time cron 触发 rebalance_to_target。
                               # 回放模式下不使用此字段（按逻辑日推进，开盘 = 当日循环开始）

        "topk",
        "n_drop",
        "risk_degree",         # 等权 cash 系数（qlib TopkDropoutStrategy 同源），默认 0.95
        "gateway",             # 如 "QMT" / "QMT_SIM"; 下单 + mixin 价格查询都用
        "output_root",         # D:/ml_output 根
        "lookback_days",
        "provider_uri",        # qlib bin 根
        "baseline_path",       # 空则用 bundle_dir/baseline.parquet
        "monitor_window_days",
        "enable_trading",      # 干跑开关
        "subprocess_timeout_s",
        # Phase 4 回放支持（仅 sim 模式生效，实盘 gateway 自动跳过）
        "enable_replay",                  # 总开关，默认 True
        "replay_start_date",              # 可选 override，空则从 bundle task.json test[0] 推导
        "replay_end_date",                # 可选 override，空则取 today-1
        "replay_skip_existing",           # 跳过已写过 diagnostics.json 的日期（重启续跑友好）
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
        # Phase 4 回放运行时状态
        "replay_status",                  # idle | running | completed | error | skipped_live
        "replay_progress",                # "23/80"
        "replay_last_done",               # 最后一个完成的逻辑日 ISO
    ]

    # Parameter defaults
    bundle_dir: str = ""
    inference_python: str = r"E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe"
    trigger_time: str = "21:00"   # 20:00 拉数后 1h, live_end=today 数据已齐 → 推理 + persist
    buy_sell_time: str = "09:26"  # 实盘 T+1 开盘交易时间 — 09:25 集合竞价开盘价后立即下单,
                                   # 提高 09:30 撮合概率. on_init 注册第二个 cron 调
                                   # run_open_rebalance: 读 T 日 pred (昨晚 21:00 已 persist)
                                   # + 当前开盘价 → rebalance + send_order.
    topk: int = 7
    n_drop: int = 1
    risk_degree: float = 0.95
    gateway: str = ""
    output_root: str = "D:/ml_output"
    lookback_days: int = 60
    provider_uri: str = ""
    baseline_path: str = ""
    monitor_window_days: int = 30
    enable_trading: bool = False
    subprocess_timeout_s: int = 180

    # Phase 4 回放参数
    enable_replay: bool = True
    replay_start_date: str = ""
    replay_end_date: str = ""
    replay_skip_existing: bool = True

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

    # Phase 4 回放运行时状态
    replay_status: str = "idle"
    replay_progress: str = ""
    replay_last_done: str = ""

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
        """日志: print 到 stdout + 发 vnpy EVENT_LOG, 让 UI LogMonitor 也能看到.

        原实现只 print, 导致 UI 右侧日志面板和 vnpy loguru 都看不到策略内部状态;
        异常路径里 _emit_failed/_emit_empty 又都是自定义事件 UI 不订阅, 等于"全黑".
        改成同时发 vnpy 标准 LogData 事件 (gateway_name=APP_NAME), MainWindow 的
        Logger handler 会自动写入 vt_YYYYMMDD.log, ML UI 的 LogMonitor 也会显示.
        """
        prefix = f"[{self.strategy_name}] " if self.strategy_name else "[MLStrategy] "
        full_msg = prefix + msg
        print(full_msg)
        try:
            event_engine = self.signal_engine.event_engine
        except AttributeError:
            return  # 单测/无 engine 场景下退化为只 print
        try:
            log = LogData(msg=full_msg, gateway_name=APP_NAME)
            event_engine.put(Event(type=EVENT_LOG, data=log))
        except Exception:
            pass

    # -----------------------------------------------------------------
    # 主入口 - 被 DailyTimeTaskScheduler 在后台线程调用
    # -----------------------------------------------------------------

    def run_daily_pipeline(self, as_of_date=None) -> None:
        """完整日频 pipeline — 主进程编排, 推理在子进程, 下单在主进程.

        Parameters
        ----------
        as_of_date : Optional[datetime.date]
            Phase 4 回放支持。给定时强制使用该日期作为 ``today``（推理子进程
            ``--live-end`` 也走该日期），让回放循环可以"假装今天是 X 日"逐日跑过历史。
            默认 None → ``date.today()``，保持实盘 trigger_time / 单日手动触发行为不变。

        try/finally 包整个函数体: 任意 return 路径 (non_trading_day/predict 异常/
        failed/empty/正常完成) 都会触发一次 ``put_strategy_event``, 让 UI variables
        表刷新最新 last_status/last_n_pred/last_error 等. 否则 UI 永远停在初始值.
        """
        try:
            today = as_of_date if as_of_date is not None else date.today()
            self.last_run_date = str(today)
            self.last_error = ""
            self.last_stage = ""

            if not self._is_trade_day(today):
                self.write_log(f"non-trading day {today}, pipeline skipped")
                self._emit_heartbeat(reason="non_trading_day")
                return

            self.last_stage = Stage.PREDICT.value
            self.write_log(
                f"pipeline start: live_end={today} bundle={self.bundle_dir} "
                f"lookback={self.lookback_days}"
            )
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
                self.write_log(f"pipeline failed at predict: {self.last_error}")
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
                err = diag.get("error_message", "subprocess failed")
                self.last_error = err
                self.write_log(f"pipeline subprocess failed: {err}")
                self._emit_failed(reason=err)
                return

            if diag.get("status") == InferenceStatus.EMPTY.value:
                self.write_log(
                    f"pipeline empty: rows=0 (检查今日数据是否已拉/qlib bin 是否覆盖 "
                    f"live_end={today}; 通常因 DailyIngestPipeline 未运行导致)"
                )
                self._emit_empty()
                self._publish_metrics(metrics)
                return

            self.last_stage = Stage.SELECT.value
            selected = self.select_topk(pred_df)
            self.write_log(
                f"selected topk={len(selected) if selected is not None else 0} "
                f"(status=ok, rows={self.last_n_pred})"
            )

            # Persist selections regardless of enable_trading — downstream UIs (Tab1
            # "最新 TopK 信号" / Tab2 "历史回溯") depend on having the per-day
            # selections.parquet on disk even in dry-run mode.
            self.last_stage = Stage.SAVE.value
            try:
                self.persist_selections(selected, as_of_date=today)
            except Exception as exc:
                self.write_log(f"persist_selections failed: {type(exc).__name__}: {exc}")

            # 双 cron 架构 (实盘 best practice): 21:00 trigger_time cron 只做推理 + persist,
            # 不在此发单. 因为 21:00 市场已休市, 用收盘后 stale tick 算的 volume 与次日开盘价
            # 偏差大, 且发单后等次日 09:30 撮合, 全程没在 09:26 用真实开盘价校准. 改为:
            #   - 21:00 cron (本方法): 推理 + persist selections.parquet → 信号已落盘
            #   - 09:26 cron (run_open_rebalance): 读昨日 persist 的 pred + 当前开盘价 →
            #                                       rebalance + send_order → 09:30 撮合
            # 这样 09:26 用真实开盘价算 volume, 撮合精度高, 与 batch replay 语义一致
            # (replay Day=T 的 rebalance 也是用 prev_day_pred + Day T 开盘价).
            #
            # 前期版本 21:00 也调 generate_orders 是错的 production 设计 (commit e96972c
            # 的 Bug 1 fix 仅修了透传 on_day, 没修语义); 现彻底删掉.
            self.write_log("21:00 cron 完成推理 + persist; 等 09:26 cron 触发 rebalance")

            self.last_stage = Stage.PUBLISH.value
            self._publish_metrics(metrics, as_of_date=today)
            self._emit_prediction(selected, metrics)
        finally:
            # 任意路径退出后都让 UI variables 刷新, 避免面板停在初始值.
            try:
                self.signal_engine.put_strategy_event(self)
            except Exception:
                pass

    # -----------------------------------------------------------------
    # 默认实现 + 子类扩展点
    # -----------------------------------------------------------------

    def _is_trade_day(self, d: date) -> bool:
        return self.signal_engine.is_trade_day(d)

    # -----------------------------------------------------------------
    # buy_sell_time cron 入口 (实盘 09:26 — 双 cron 架构)
    # -----------------------------------------------------------------

    def run_open_rebalance(self, as_of_date: Optional[date] = None) -> None:
        """09:26 cron 入口: 读上一交易日 21:00 cron persist 的 pred + 当前开盘价 → rebalance.

        实盘双 cron 架构的"执行"半边. 与 ``run_daily_pipeline`` (推理半边) 协作:

          T-1 21:00 cron → run_daily_pipeline → 推理 + persist predictions.parquet
          T   09:26 cron → run_open_rebalance  → 读 T-1 pred + T 开盘价 → rebalance + send_order
          T   09:30 撮合
          T   EOD settle (gateway auto via natural day rollover)
          T   21:00 cron → run_daily_pipeline → 推理 + persist (循环)

        与 ``_replay_loop_iter`` 的等价性:
          replay[Day=T] 用 prev_day_pred (T-1 pred 在内存) + on_day=T 撮合
          live[T 09:26]  用 prev_day_pred (T-1 pred 从 disk 读) + on_day=T 撮合
        两者输入一致 → sim_trades byte-equal.

        ``as_of_date`` 给定时为逻辑日, 默认 None → date.today() (实盘 cron 路径).
        smoke fast-forward 时 as_of_date=回放日.
        """
        try:
            today = as_of_date if as_of_date is not None else date.today()
            self.last_run_date = str(today)
            self.last_stage = ""
            self.last_error = ""

            if not self._is_trade_day(today):
                self.write_log(f"[open_rebalance] non-trading day {today}, skip")
                return

            if not self.enable_trading:
                self.write_log("[open_rebalance] enable_trading=False, skip (dry-run)")
                return

            # 1. 解析"上一交易日" — 用 qlib calendar (provider_uri) 跳过周末/节假日
            from .utils.trade_calendar import make_calendar
            cal = make_calendar(self.provider_uri)
            prev_day = cal.prev_trade_day(today)
            if prev_day is None:
                self.write_log(f"[open_rebalance] {today}: 找不到上一交易日 (calendar 起点)")
                return

            # 2. 读 prev_day 的 predictions.parquet (= T-1 21:00 cron persist 的)
            prev_day_str = prev_day.strftime("%Y%m%d")
            prev_pred_path = (
                Path(self.output_root) / self.strategy_name / prev_day_str / "predictions.parquet"
            )
            if not prev_pred_path.exists():
                self.write_log(
                    f"[open_rebalance] {today}: 上日 {prev_day} pred 缺失 ({prev_pred_path}); "
                    f"昨晚 21:00 cron 是否成功跑过?"
                )
                return

            try:
                pred_df = pd.read_parquet(prev_pred_path)
            except Exception as exc:
                self.write_log(f"[open_rebalance] 读 prev pred 失败: {exc}")
                return

            # 3. 提取 prev_day pred_score
            try:
                last_dt = pred_df.index.get_level_values("datetime").max()
                pred_score = pred_df.xs(last_dt, level="datetime")
                if isinstance(pred_score, pd.DataFrame):
                    pred_score = pred_score.iloc[:, 0]
            except Exception as exc:
                self.write_log(f"[open_rebalance] 提取 pred_score 失败: {exc}")
                return

            if pred_score is None or pred_score.empty:
                self.write_log(f"[open_rebalance] prev_day {prev_day} pred 为空")
                return

            # 4. 刷新今日开盘价 tick (refresh_tick(vt, as_of_date=today))
            #    以"当前持仓 + prev pred top-k 候选"为目标 — 覆盖 sells/buys 全集
            top_candidates = pred_score.sort_values(ascending=False).head(self.topk).index
            candidate_vts: List[str] = []
            for inst in top_candidates:
                vt = self._instrument_to_vt(str(inst))
                if vt:
                    candidate_vts.append(vt)
            self._refresh_market_data_for_day(today, candidates=candidate_vts)

            # 5. rebalance — 用 prev_day pred (= T-1 21:00 推理结果) + 今日开盘价
            self.last_stage = Stage.ORDER.value
            self.write_log(
                f"[open_rebalance] {today}: 用 prev_day={prev_day} pred (n={len(pred_score)}) rebalance"
            )
            self.rebalance_to_target(pred_score, on_day=today)
        finally:
            try:
                self.signal_engine.put_strategy_event(self)
            except Exception:
                pass

    def select_topk(self, pred_df: pd.DataFrame) -> pd.DataFrame:
        if pred_df is None or pred_df.empty:
            return pd.DataFrame()
        last_dt = pred_df.index.get_level_values("datetime").max()
        slice_df = pred_df.xs(last_dt, level="datetime")
        return slice_df.sort_values("score", ascending=False).head(self.topk)

    def persist_selections(self, selected: pd.DataFrame, as_of_date: Optional[date] = None) -> None:
        """Write selections.parquet to {output_root}/{name}/{yyyymmdd}/.

        Default impl writes canonical schema (trade_date, instrument, rank,
        score, weight, target_price, side, model_run_id). Subclasses may
        override to customize (e.g. add sector weights, risk budgeting).

        Always runs — does not depend on ``enable_trading``.

        Phase 4 回放支持：``as_of_date`` 给定时按该日期写入子目录与 trade_date 列；
        默认 None → date.today()，兼容实盘 trigger_time 路径。
        """
        from .persistence.result_store import ResultStore
        from .persistence.schema import (
            COL_INSTRUMENT, COL_MODEL_RUN_ID, COL_NAME, COL_RANK, COL_SIDE,
            COL_TARGET_PRICE, COL_TRADE_DATE, COL_WEIGHT,
        )
        if selected is None or selected.empty:
            return
        today = as_of_date if as_of_date is not None else date.today()
        sel_df = selected.reset_index().copy()
        sel_df[COL_TRADE_DATE] = today.strftime("%Y-%m-%d")
        sel_df[COL_INSTRUMENT] = sel_df.get("instrument", sel_df.iloc[:, 0])
        sel_df[COL_RANK] = range(1, len(sel_df) + 1)
        # qlib TopkDropoutStrategy 的目标权重 = risk_degree / topk
        # (公式: buy_amount = floor(cash × risk_degree / n_buys / open / 100) × 100)
        # 之前 1.0/len(sel_df) 漏乘 risk_degree，前端显示 14.29% 误导用户以为满仓
        sel_df[COL_WEIGHT] = float(self.risk_degree) / len(sel_df)
        sel_df[COL_TARGET_PRICE] = float("nan")
        sel_df[COL_SIDE] = "long"
        sel_df[COL_MODEL_RUN_ID] = self.last_model_run_id
        # 写时 enrichment: ts_code → 股票中文简称. 失败不阻断落盘 (selections
        # 数据可用性优先于 name 装饰), 缺失时 name 为 None.
        try:
            from vnpy_tushare_pro.ml_data_build import enrich_with_name
            sel_df = enrich_with_name(sel_df, code_col=COL_INSTRUMENT, name_col=COL_NAME)
        except Exception as exc:  # noqa: BLE001
            self.write_log(f"stock name enrich failed (non-fatal): {type(exc).__name__}: {exc}")
            sel_df[COL_NAME] = None
        store = ResultStore(self.output_root)
        store.write_selections(self.strategy_name, today, sel_df)

    def generate_orders(
        self,
        pred_score: pd.Series,
        selected: pd.DataFrame,
        on_day: Optional[date] = None,
    ) -> None:
        """子类实现. 内部调 self.send_order(...). 仅在 enable_trading=True 时被调用.

        ``on_day`` 给定时为 pipeline 的逻辑日 (= run_daily_pipeline 的 ``today``,
        即 ``as_of_date or date.today()``). 子类必须把它透传给 ``rebalance_to_target``
        和参考价查询, 否则 smoke / 历史回放场景下 on_day 会错用 wall-clock today,
        log 显示乱日期 + 撮合可能取错日的参考价.

        Phase 6: 签名加 pred_score (Series, 全量) 作为第一参数 — qlib TopkDropoutStrategy
        算法需要全量候选池才能正确决策。子类典型实现：

            def generate_orders(self, pred_score, selected, on_day=None):
                if not self.enable_trading:
                    return
                self.rebalance_to_target(pred_score, on_day=on_day or date.today())

        ``selected`` 仍然传入用于子类做日志 / persistence，但 rebalance 内部不再使用它。
        """
        raise NotImplementedError("subclass should implement generate_orders")

    # -----------------------------------------------------------------
    # 调仓 — diff: sells (持仓 - 信号) + buys (信号 - 持仓)
    # -----------------------------------------------------------------
    # 实盘模式：T 日 21:00 推理产出 topk → T+1 日 09:30 调 rebalance_to_target
    # 回放模式：T 日 在 _replay_apply_day 暂存 topk → T+1 日开头调 rebalance（详见 _run_replay_loop）

    def rebalance_to_target(
        self,
        pred_score: pd.Series,
        on_day: Optional[date] = None,
    ) -> Dict[str, Any]:
        """用 qlib TopkDropoutStrategy 算法决定 sells/buys，再走 vnpy 撮合。

        Phase 6 改造：决策算法从手写 strict diff 换成
        :func:`vnpy_ml_strategy.topk_dropout_decision.topk_dropout_decision`，
        与 qlib ``TopkDropoutStrategy.generate_trade_decision`` 算法等价。

        Parameters
        ----------
        pred_score : pd.Series
            **全量** 当日预测分数, index = instrument (ts_code 格式如 "000001.SZ"),
            value = score。**不是** select_topk 后的 head(topk) — 算法需要看
            完整候选池才能正确选 today 候选 (`top of ~last`).
        on_day : date, optional
            执行交易的逻辑日。回放传当日；实时不传走 today。

        Returns
        -------
        dict: {sells_dispatched, buys_dispatched, sells_skipped, buys_skipped}
        """
        from .topk_dropout_decision import topk_dropout_decision

        on_day = on_day or date.today()
        stats = {"sells_dispatched": 0, "buys_dispatched": 0, "sells_skipped": 0, "buys_skipped": 0}

        # 边界：无预测 → 不调仓
        if pred_score is None or len(pred_score) == 0:
            self.write_log(f"[rebalance] on_day={on_day} pred_score 为空，跳过")
            return stats

        # 当前持仓 (vt_symbol → PositionData)
        positions = self._get_long_positions()

        # vt_symbol ↔ ts_code 双向映射（算法用 ts_code，撮合用 vt_symbol）
        ts_to_vt: Dict[str, str] = {}
        current_holdings_ts: List[str] = []
        for vt in positions.keys():
            ts = self._vt_to_instrument(vt)
            if ts:
                ts_to_vt[ts] = vt
                current_holdings_ts.append(ts)

        # is_tradable callback：md.tick.last_price 与 limit_up/limit_down 比较
        gateway = self._get_own_gateway()
        md = getattr(gateway, "md", None) if gateway is not None else None

        def is_tradable(ts_code: str, direction: Optional[str]) -> bool:
            vt = self._instrument_to_vt(ts_code)
            if vt is None or md is None:
                return False
            tick = md.get_full_tick(vt)
            if tick is None:
                return False
            last = float(getattr(tick, "last_price", 0) or 0)
            if last <= 0:
                return False
            limit_up = float(getattr(tick, "limit_up", 0) or 0)
            limit_down = float(getattr(tick, "limit_down", 0) or 0)
            # 1e-4 容差防浮点等于
            at_up = limit_up > 0 and last >= limit_up - 1e-4
            at_down = limit_down > 0 and last <= limit_down + 1e-4
            if direction is None:
                return not (at_up or at_down)
            if direction == "BUY":
                return not at_up  # 涨停不能买
            if direction == "SELL":
                return not at_down  # 跌停不能卖
            return True

        # 调 qlib 算法决策（与训练时回测算法等价）
        sell_ts, buy_ts = topk_dropout_decision(
            pred_score=pred_score,
            current_holdings=current_holdings_ts,
            topk=self.topk,
            n_drop=self.n_drop,
            method_buy="top",
            method_sell="bottom",
            only_tradable=True,
            forbid_all_trade_at_limit=True,  # 与 qlib 默认一致
            hold_thresh=1,  # A 股 T+1 天然满足
            is_tradable=is_tradable,
        )

        # ts_code 转 vt_symbol
        sell_vts = [ts_to_vt[ts] for ts in sell_ts if ts in ts_to_vt]
        buy_vts = []
        for ts in buy_ts:
            vt = self._instrument_to_vt(ts)
            if vt:
                buy_vts.append(vt)

        self.write_log(
            f"[rebalance] on_day={on_day} pred_n={len(pred_score)} "
            f"current={len(current_holdings_ts)} → sells={len(sell_vts)} buys={len(buy_vts)} "
            f"(qlib TopkDropout: topk={self.topk} n_drop={self.n_drop})"
        )

        # 1. sells（先卖释放资金）
        for vt in sell_vts:
            pos = positions.get(vt)
            if pos is None:
                stats["sells_skipped"] += 1
                continue
            sell_volume = float(getattr(pos, "yd_volume", 0) or 0)
            if sell_volume <= 0:
                stats["sells_skipped"] += 1
                self.write_log(f"[rebalance] skip sell {vt}: yd_volume=0 (T+1 限制)")
                continue
            try:
                self.send_order(
                    vt_symbol=vt,
                    direction=Direction.SHORT,
                    offset=Offset.CLOSE,
                    price=0.0,
                    volume=sell_volume,
                    order_type=OrderType.MARKET,
                )
                stats["sells_dispatched"] += 1
            except Exception as exc:
                self.write_log(f"[rebalance] sell {vt} 失败: {type(exc).__name__}: {exc}")
                stats["sells_skipped"] += 1

        # 2. buys（后买，qlib 等权 + risk_degree 公式）
        # n_buys = 算法选出的 buy list 长度（不是过滤后的）— 与 qlib value =
        # cash * risk / len(buy) 的分母一致
        priced_buys: List[Tuple[str, float]] = []
        for vt in buy_vts:
            ref_price = self._get_reference_price(vt)
            if ref_price is None or ref_price <= 0:
                stats["buys_skipped"] += 1
                self.write_log(f"[rebalance] skip buy {vt}: 无参考价")
                continue
            priced_buys.append((vt, float(ref_price)))

        # qlib 公式 value = cash × risk_degree / len(buy)，buy 是算法输出长度
        # 不是 priced_buys 长度（避免分母被无价的剔除而失真）
        n_buys_qlib = len(buy_vts)
        if n_buys_qlib > 0 and priced_buys:
            current_cash = self._get_current_cash()
            for vt, ref_price in priced_buys:
                volume = self._calculate_buy_amount(ref_price, current_cash, n_buys_qlib)
                if volume <= 0:
                    stats["buys_skipped"] += 1
                    self.write_log(
                        f"[rebalance] skip buy {vt}: cash={current_cash:.2f} risk={self.risk_degree} "
                        f"n_buys={n_buys_qlib} price={ref_price} → volume={volume}"
                    )
                    continue
                try:
                    self.send_order(
                        vt_symbol=vt,
                        direction=Direction.LONG,
                        offset=Offset.OPEN,
                        price=0.0,
                        volume=volume,
                        order_type=OrderType.MARKET,
                    )
                    stats["buys_dispatched"] += 1
                except Exception as exc:
                    self.write_log(f"[rebalance] buy {vt} 失败: {type(exc).__name__}: {exc}")
                    stats["buys_skipped"] += 1

        self.write_log(
            f"[rebalance] done {stats['sells_dispatched']}+{stats['buys_dispatched']} sent "
            f"({stats['sells_skipped']} sell-skipped, {stats['buys_skipped']} buy-skipped)"
        )
        return stats

    def _get_long_positions(self) -> Dict[str, Any]:
        """从 main_engine 拿本策略 gateway 的 LONG 持仓 (volume>0)。返回 dict: vt_symbol → PositionData。"""
        positions: Dict[str, Any] = {}
        try:
            main_engine = getattr(self.signal_engine, "main_engine", None)
            if main_engine is None:
                return positions
            for pos in main_engine.get_all_positions():
                if getattr(pos, "gateway_name", "") != self.gateway:
                    continue
                # 注意：vnpy Direction 是 Enum；用 enum 直接比较或比较 .value，不能用 str(enum)
                # （str(Direction.LONG) = "Direction.LONG" ≠ "多" = Direction.LONG.value）
                pos_dir = getattr(pos, "direction", None)
                if pos_dir != Direction.LONG and getattr(pos_dir, "value", None) != Direction.LONG.value:
                    continue
                if float(getattr(pos, "volume", 0) or 0) <= 0:
                    continue
                positions[pos.vt_symbol] = pos
        except Exception as exc:
            self.write_log(f"_get_long_positions 失败: {exc}")
        return positions

    def _get_reference_price(self, vt_symbol: str) -> Optional[float]:
        """拿当日参考价（reference_kind=today_open 时为当日 open）用于算买入手数。

        关键：直接从 gateway.md.get_full_tick(vt_symbol) 读，**不**走 main_engine.get_tick。
        因为 md.refresh_tick / set_synthetic_tick 只写 md._tick_cache，从不调
        gateway.on_tick() 推到 vnpy OMS → main_engine.get_tick 在回放期间永远返 None。

        与 Phase 5.1 撮合层 td._resolve_trade_price 走同一条 md 缓存路径，保证读价 +
        撮合价口径完全一致。

        实盘：vnpy 真实 gateway 会持续 push tick，main_engine 兜底也能命中；这里
        优先级是 md._tick_cache → main_engine.get_tick (兜底)。
        """
        try:
            gateway = self._get_own_gateway()
            if gateway is not None:
                md = getattr(gateway, "md", None)
                if md is not None and hasattr(md, "get_full_tick"):
                    tick = md.get_full_tick(vt_symbol)
                    if tick is not None:
                        last = float(getattr(tick, "last_price", 0) or 0)
                        if last > 0:
                            return last
                        pre = float(getattr(tick, "pre_close", 0) or 0)
                        if pre > 0:
                            return pre
            # 实盘兜底：md 缓存命中失败时尝试 main_engine OMS
            main_engine = getattr(self.signal_engine, "main_engine", None)
            if main_engine is None:
                return None
            tick = main_engine.get_tick(vt_symbol)
            if tick is not None:
                last = float(getattr(tick, "last_price", 0) or 0)
                if last > 0:
                    return last
                pre = float(getattr(tick, "pre_close", 0) or 0)
                if pre > 0:
                    return pre
        except Exception:
            pass
        return None

    def _calculate_buy_amount(
        self,
        ref_price: float,
        current_cash: float,
        n_buys: int,
        trade_unit: int = 100,
    ) -> int:
        """qlib TopkDropoutStrategy 等权 + risk_degree 公式 (signal_strategy.py:266-286 同源):
            value = cash × risk_degree / n_buys
            amount = value / ref_price
            return floor(amount / trade_unit) × trade_unit

        ref_price 是 daily_merged 的**原始 open**（未复权），不是 hfq。
        与决策 1"撮合用原始价"一致：资金占用 ≈ value，手数与 qlib backtest 不同
        （hfq_open 长期累乘除权后可能差几倍），但起点 value₀ ≈ value，之后都按
        pct_chg 累乘 → 权益曲线绝对水平基本一致（决策 2 的 pct_chg mark-to-market）。
        """
        if ref_price <= 0 or n_buys <= 0 or current_cash <= 0:
            return 0
        value = float(current_cash) * float(self.risk_degree) / float(n_buys)
        amount = value / float(ref_price)
        lots = int(amount // trade_unit)
        return max(0, lots * trade_unit)

    def _get_current_cash(self) -> float:
        """从 main_engine 拿本策略 gateway 当前可用现金（balance - frozen）。

        rebalance buys 阶段调用：sells 已被同步撮合（vnpy_qmt_sim 同步成交），
        cash 已包含卖出回款。返 0 时上层会拒绝下单。

        优先级：
          1. vnpy_qmt_sim 直读 counter.capital — sim 设计为"send_order 同步成交"
             ([td.py:500-501] match_order 同步在 send_order 内执行 + 同步更新
             counter.capital)，但 _emit_account → EventEngine → OmsEngine 链路
             引入异步 1-tick 延迟。rebalance 主线程 sells 完后立即调本函数时,
             OmsEngine.accounts 仍是旧值 → 算出的 cash 缺 sell 回款 → buy 投入
             偏小。直读 counter 绕过这条 event 链，拿到真实同步 cash。
          2. fallback OmsEngine event 路径 — 实盘 gateway (QMT/miniqmt) 没有
             counter 字段，自动走这里。实盘 sell 撮合本来就异步（订单到券商等
             回报），OmsEngine 异步更新是符合实盘真实语义的，无需特殊处理。
        """
        try:
            main_engine = getattr(self.signal_engine, "main_engine", None)
            if main_engine is None:
                return 0.0
            # Fast path: vnpy_qmt_sim counter 同步直读
            try:
                gateway = main_engine.get_gateway(self.gateway)
            except Exception:
                gateway = None
            counter = getattr(getattr(gateway, "td", None), "counter", None) if gateway else None
            if counter is not None and hasattr(counter, "capital"):
                cap = float(getattr(counter, "capital", 0) or 0)
                frz = float(getattr(counter, "frozen", 0) or 0)
                return max(0.0, cap - frz)
            # Fallback: OmsEngine event-based 路径（实盘）
            for acc in main_engine.get_all_accounts():
                if getattr(acc, "gateway_name", "") == self.gateway:
                    balance = float(getattr(acc, "balance", 0) or 0)
                    frozen = float(getattr(acc, "frozen", 0) or 0)
                    return max(0.0, balance - frozen)
        except Exception:
            return 0.0
        return 0.0

    def _instrument_to_vt(self, instrument: str) -> Optional[str]:
        """tushare ts_code (000001.SZ) → vnpy vt_symbol (000001.SZSE). 子类可覆写。"""
        if not instrument:
            return None
        if instrument.endswith(".SZ"):
            return instrument[:-3] + ".SZSE"
        if instrument.endswith(".SH"):
            return instrument[:-3] + ".SSE"
        if instrument.endswith(".BJ"):
            return instrument[:-3] + ".BSE"
        # 已是 vt_symbol 格式
        if "." in instrument:
            return instrument
        return None

    def _vt_to_instrument(self, vt: str) -> Optional[str]:
        """vnpy vt_symbol (000001.SZSE) → tushare ts_code (000001.SZ)。"""
        if not vt or "." not in vt:
            return None
        sym, ex = vt.rsplit(".", 1)
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(ex.upper())
        if suffix is None:
            return None
        return f"{sym}.{suffix}"

    # -----------------------------------------------------------------
    # 事件发送
    # -----------------------------------------------------------------

    def _publish_metrics(self, metrics: Dict[str, Any], as_of_date: Optional[date] = None) -> None:
        """Phase 4 回放支持：as_of_date 给定时按该日期发指标，默认 today。"""
        self.signal_engine.publish_metrics(
            strategy_name=self.strategy_name,
            metrics=metrics,
            trade_date=as_of_date if as_of_date is not None else date.today(),
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
        """策略初始化 — 注册双 cron + 校验 bundle 完整性.

        实盘双 cron 设计 (commit 后续):
          - {strategy_name}_predict   @ trigger_time   (默认 21:00) → run_daily_pipeline (推理 + persist)
          - {strategy_name}_rebalance @ buy_sell_time  (默认 09:26) → run_open_rebalance (rebalance + send_order)

        历史 job 命名约定 ``strategy_name`` 沿用为 _predict 后缀 (向后兼容
        ``run_pipeline_now`` 的入口语义).
        """
        self.signal_engine.register_predict_job(
            strategy_name=self.strategy_name,
            trigger_time=self.trigger_time,
            callback=self.run_daily_pipeline,
        )
        self.signal_engine.register_rebalance_job(
            strategy_name=self.strategy_name,
            buy_sell_time=self.buy_sell_time,
            callback=self.run_open_rebalance,
        )
        self.signal_engine.validate_bundle(self.bundle_dir)
        self.inited = True
        self.write_log(
            f"on_init completed; cron 已注册: predict@{self.trigger_time} + rebalance@{self.buy_sell_time}"
        )

    def on_start(self) -> None:
        self.trading = True
        self.write_log("on_start")
        # Phase 4：仅 sim 模式策略启动后台线程跑回放（不阻塞 on_start 返回）
        try:
            self._start_replay_if_needed()
        except Exception as exc:
            # 回放启动失败不影响策略实时模式；记日志即可
            self.write_log(f"_start_replay_if_needed failed: {exc}")

    def on_stop(self) -> None:
        self.trading = False
        # 双 cron 都要 unregister
        self.signal_engine.unregister_daily_job(strategy_name=self.strategy_name + "_predict")
        self.signal_engine.unregister_daily_job(strategy_name=self.strategy_name + "_rebalance")
        self.write_log("on_stop")

    # -----------------------------------------------------------------
    # Phase 4: 模拟模式加速回放
    # -----------------------------------------------------------------
    # 详见 plan 文档 Phase 4 章节。核心思路：
    #   1. 仅 sim 模式（gateway 名以 "QMT_SIM" 开头）启用，实盘自动跳过
    #   2. 起止日自动从 bundle task.json 推导，零配置可用
    #   3. 后台线程跑 + 暂停本策略 cron + 禁本 gateway 自动 settle，
    #      避免与 trigger_time 实时任务并发或被 timer 自然日切换污染
    #   4. 用 output_root/{name}/{day_str}/diagnostics.json 做 idempotency

    def _resolve_replay_window(self) -> Optional[Tuple[date, date]]:
        """从 setting + bundle task.json 推导回放起止日期。返回 None 表示跳过。"""
        if not self.enable_replay:
            return None

        # 起点：setting override > bundle task.json test[0]
        if self.replay_start_date:
            start = datetime.strptime(self.replay_start_date, "%Y-%m-%d").date()
        else:
            task_path = Path(self.bundle_dir) / "task.json"
            try:
                task_json = json.loads(task_path.read_text(encoding="utf-8"))
                test_segment = task_json["dataset"]["kwargs"]["segments"]["test"]
                # tushare segment 可能是 ISO 字符串或 [yyyy, mm, dd] 列表，统一转 date
                start_raw = test_segment[0]
                if isinstance(start_raw, str):
                    start = datetime.strptime(start_raw[:10], "%Y-%m-%d").date()
                elif isinstance(start_raw, (list, tuple)) and len(start_raw) == 3:
                    start = date(*start_raw)
                else:
                    raise ValueError(f"无法解析 task.json test[0]={start_raw!r}")
            except Exception as exc:
                self.write_log(f"读 bundle task.json 推导 replay_start 失败: {exc}")
                return None

        # 终点：setting override > today - 1
        if self.replay_end_date:
            end = datetime.strptime(self.replay_end_date, "%Y-%m-%d").date()
        else:
            end = date.today() - timedelta(days=1)

        # 自动 cap 到 qlib calendar 末尾日 — 否则 today-1 默认值在 ingest
        # 滞后时会触发 StaleCalendarError (workday > calendar max_known).
        # 显式 replay_end_date 也会被 cap, 防止用户配错把超出数据范围的天传进去.
        try:
            from .utils.trade_calendar import make_calendar
            cal = make_calendar(self.provider_uri)
            max_known_str = getattr(cal, "_max_known", None)
            if max_known_str is None:
                # WeekdayFallbackCalendar 没有 _max_known 也别强制加载
                load = getattr(cal, "_load", None)
                if callable(load):
                    load()
                    max_known_str = getattr(cal, "_max_known", None)
            if max_known_str:
                cal_end = datetime.strptime(max_known_str, "%Y-%m-%d").date()
                if end > cal_end:
                    self.write_log(
                        f"[replay] end={end} 超过 qlib calendar 末尾 {cal_end}, "
                        f"自动 cap 到 {cal_end} (避免 StaleCalendarError)"
                    )
                    end = cal_end
        except Exception as exc:
            self.write_log(f"[replay] cap end 到 calendar 末尾失败 (continuing): {exc}")

        if start > end:
            # 已经追上实时（如新部署的 bundle 即时启动），无需回放
            return None
        return start, end

    def _start_replay_if_needed(self) -> None:
        """on_start 末尾调用。仅 sim 模式启用回放，实盘自动跳过。"""
        from vnpy_common.naming import classify_gateway

        if classify_gateway(self.gateway) != "sim":
            self.replay_status = "skipped_live"
            self.write_log(
                f"gateway={self.gateway!r} 非模拟模式，跳过回放，"
                f"直接进入每日 trigger_time 实时推理"
            )
            return

        # 显式 override 校验（隐式推导永远不会触发此 case）
        if self.replay_start_date:
            try:
                self._validate_explicit_replay_start()
            except ValueError as exc:
                self.replay_status = "error"
                self.last_error = str(exc)
                self.write_log(f"回放参数校验失败: {exc}")
                return

        window = self._resolve_replay_window()
        if window is None:
            self.replay_status = "skipped_live"
            self.write_log("回放窗口为空（已追上实时或 enable_replay=False），跳过")
            return

        self.replay_status = "running"
        self.write_log(
            f"[replay] resolved window: {window[0]} ~ {window[1]} "
            f"(将自然日 {(window[1] - window[0]).days + 1}d 内逐交易日回放)"
        )
        threading.Thread(
            target=self._run_replay_loop,
            args=window,
            name=f"replay-{self.strategy_name}",
            daemon=True,
        ).start()

    def _validate_explicit_replay_start(self) -> None:
        """显式 replay_start_date 校验：必须 ≥ bundle test_start，防历史泄漏。"""
        explicit = datetime.strptime(self.replay_start_date, "%Y-%m-%d").date()
        task_path = Path(self.bundle_dir) / "task.json"
        task_json = json.loads(task_path.read_text(encoding="utf-8"))
        test_segment = task_json["dataset"]["kwargs"]["segments"]["test"]
        start_raw = test_segment[0]
        if isinstance(start_raw, str):
            test_start = datetime.strptime(start_raw[:10], "%Y-%m-%d").date()
        else:
            test_start = date(*start_raw)
        if explicit < test_start:
            raise ValueError(
                f"显式 replay_start_date={self.replay_start_date} 早于 bundle "
                f"test_start={test_start}，会发生历史数据泄漏（验证集训练数据被用作回放）。"
            )

    def _run_replay_loop(self, start: date, end: date) -> None:
        """后台线程跑回放循环。详见 plan 文档 Phase 4 边界控制。

        关键修复 (holdings diverge 根因): 必须确保 trade_calendar 已注入,
        否则 _is_trade_day fallback 周一到周五 → 春节/国庆/五一等节假日被
        误判为交易日 → 回放跑无效 rebalance → 持仓与 qlib backtest diverge。
        """
        # 注入 qlib 日历 (用 strategy.provider_uri, 与 inference 同源)
        ensure_cal = getattr(self.signal_engine, "ensure_trade_calendar", None)
        if callable(ensure_cal):
            try:
                ensure_cal(self.provider_uri)
            except Exception as exc:
                self.write_log(f"[replay] 注入 trade_calendar 失败 (将 fallback 周一到周五, 节假日会误判): {exc}")

        scheduler = getattr(self.signal_engine, "scheduler", None)
        gateway = self._get_own_gateway()

        # 1. 暂停本策略两个 APScheduler cron (按 strategy_name 隔离, 不影响其他策略)
        # 双 cron 架构: predict (21:00) + rebalance (09:26) 都需要暂停, 防回放期间
        # 真实 cron 与回放并发触发推理 / rebalance.
        paused_jobs: List[str] = []
        if scheduler is not None:
            for suffix in ("_predict", "_rebalance"):
                job_name = self.strategy_name + suffix
                try:
                    scheduler.pause_job(job_name)
                    paused_jobs.append(job_name)
                except Exception as exc:
                    self.write_log(f"[replay] pause_job({job_name}) 失败 (continuing): {exc}")
            self.write_log(f"[replay] 暂停 cron jobs {paused_jobs}")

        # 2. 禁用本 gateway 自动 settle（仅按 gateway 实例隔离）
        if gateway is not None:
            try:
                gateway.enable_auto_settle(False)
                self.write_log(f"[replay] 禁用 gateway={self.gateway} 自动 settle")
            except Exception as exc:
                self.write_log(f"[replay] enable_auto_settle(False) 失败: {exc}")

        try:
            self._replay_loop_body(start, end, gateway)
            self.replay_status = "completed"
            self.write_log("[replay] 全部交易日完成，进入实时模式")
        except Exception as exc:
            self.replay_status = "error"
            self.last_error = f"replay_loop: {type(exc).__name__}: {exc}"
            self.write_log(f"[replay] 异常退出: {self.last_error}")
        finally:
            # 4. 恢复 gateway 自动 settle
            if gateway is not None:
                try:
                    gateway.enable_auto_settle(True)
                except Exception:
                    pass
            # 5. 恢复本策略两个 cron
            if scheduler is not None:
                for job_name in paused_jobs:
                    try:
                        scheduler.resume_job(job_name)
                    except Exception as exc:
                        self.write_log(f"[replay] resume_job({job_name}) 失败: {exc}")
                if paused_jobs:
                    self.write_log(f"[replay] 恢复 cron jobs {paused_jobs}")

    def _replay_loop_body(self, start: date, end: date, gateway: Any) -> None:
        """Phase 4 加速回放主循环。

        架构（vs 原逐日 spawn 子进程版）：
          1. 一次性批量推理：调 run_inference_range 一个子进程跑完 [start, end]，
             写每日 {output_root}/{name}/{yyyymmdd}/predictions.parquet + diagnostics.json
             加速 ~10-20x（省掉 N 次 qlib 加载 + 子进程启动）
          2. 逐日循环 in-process apply：读已写的 predictions.parquet → select_topk →
             persist_selections → generate_orders（如 enable_trading）→ settle_end_of_day
             不再 spawn 子进程，每天纯内存计算 + IO，~ms 级
        """
        days: List[date] = []
        cursor = start
        while cursor <= end:
            if self._is_trade_day(cursor):
                days.append(cursor)
            cursor += timedelta(days=1)

        total = len(days)
        if total == 0:
            self.write_log(f"[replay] 起止 {start} ~ {end} 内无交易日，跳过")
            return

        # Phase A: 批量推理（一次子进程产出所有日 predictions/diagnostics）
        # 跳过已有 batch_mode diagnostics 的窗口（续跑幂等）
        need_batch_predict = self._need_batch_predict(days)
        if need_batch_predict:
            self.write_log(f"[replay] batch predict {start} ~ {end} ({total} 交易日)，spawning 一个推理子进程...")
            try:
                stats = self.signal_engine.run_inference_range(
                    bundle_dir=self.bundle_dir,
                    range_start=start,
                    range_end=end,
                    lookback_days=self.lookback_days,
                    strategy_name=self.strategy_name,
                    inference_python=self.inference_python,
                    output_root=self.output_root,
                    provider_uri=self.provider_uri,
                    baseline_path=self.baseline_path or None,
                    timeout_s=max(3600, total * 30),  # 给充足余量
                )
                self.write_log(
                    f"[replay] batch predict done: {stats.get('n_days_with_data')} days have data "
                    f"of {stats.get('n_days_total')} total (returncode={stats.get('returncode')})"
                )
                if stats.get("returncode") != 0:
                    err = stats.get("stderr_tail", "")
                    self.write_log(f"[replay] batch subprocess returned non-zero. stderr tail:\n{err}")
            except Exception as exc:
                self.write_log(
                    f"[replay] batch predict 异常: {type(exc).__name__}: {exc} — "
                    f"会逐日 fallback 到单日 run_pipeline_now"
                )
        else:
            self.write_log("[replay] 已有 batch_mode diagnostics 覆盖整个窗口，跳过批量推理（续跑）")

        # Phase B: 逐日推进
        # 真实策略时序：T 日 20:00 推理 → T+1 日 09:30 开盘 rebalance → T+1 日收盘 settle
        # 回放映射：day[i-1] 推理产出的 pred_score = day[i] 的决策依据
        #   for day in days:
        #     1. 用 prev_day_pred_score 在 day 开盘走 qlib 算法决策 → rebalance
        #     2. 读 day 的 predictions.parquet → 暂存全量 pred_score 为 day+1 决策依据
        #     3. 显式 settle_end_of_day(day)：今日买入转 yd 给 day+1 卖出
        prev_day_pred_score: Optional[pd.Series] = None

        # Phase 4：禁用本 gateway 自动 settle（按真实自然日触发会污染回放状态）
        # 回放期间由本循环显式 settle_end_of_day(day)。仅影响本 gateway 实例。
        if gateway is not None and hasattr(gateway, "enable_auto_settle"):
            try:
                gateway.enable_auto_settle(False)
                self.write_log("[replay] 已禁用本 gateway 自动 settle（按逻辑日显式结算）")
            except Exception as exc:
                self.write_log(f"[replay] 禁用 auto_settle 失败: {exc}")

        try:
            self._replay_loop_iter(days, total, prev_day_pred_score, gateway)
        finally:
            if gateway is not None and hasattr(gateway, "enable_auto_settle"):
                try:
                    gateway.enable_auto_settle(True)
                    self.write_log("[replay] 已恢复本 gateway 自动 settle")
                except Exception as exc:
                    self.write_log(f"[replay] 恢复 auto_settle 失败: {exc}")
            # 清理回放逻辑日：恢复实时模式 datetime.now() 行为
            if gateway is not None:
                try:
                    gateway.td.counter._replay_now = None
                except Exception:
                    pass

    def _replay_loop_iter(
        self,
        days: List[date],
        total: int,
        prev_day_pred_score: Optional[pd.Series],
        gateway: Any,
    ) -> None:
        """从 _replay_loop_body 拆出来的逐日 apply 循环。
        独立成函数以便上层用 try/finally 保护 gateway.enable_auto_settle 状态。

        Phase 6: 暂存对象从 prev_day_topk (head DataFrame) 改为 prev_day_pred_score
        (Series, 全量) — qlib TopkDropoutStrategy 算法需要看完整 pred_score 才能正确
        选 today 候选池。
        """
        for i, day in enumerate(days):
            day_str = day.strftime("%Y%m%d")
            day_iso = day.strftime("%Y-%m-%d")

            # 设回放逻辑日：让 vnpy_qmt_sim 撮合产生的 trade.datetime / order.datetime
            # 都用回放日（如 2026-01-06 09:30），而不是 wall-clock now（5.1 19:40）。
            if gateway is not None:
                from datetime import datetime as _dt, time as _time
                try:
                    gateway.td.counter._replay_now = _dt.combine(day, _time(9, 30, 0))
                except Exception:
                    pass

            # 1. 用上一交易日的 pred_score 在今日开盘 rebalance（qlib 算法）
            if prev_day_pred_score is not None and self.enable_trading:
                try:
                    # 候选股 vt 集合（用于刷新行情）：取 pred_score 中 score 排名前 topk 的
                    # 加上当前持仓（覆盖算法可能 sell/buy 的所有股）
                    top_candidates = prev_day_pred_score.sort_values(ascending=False).head(self.topk).index
                    candidate_vts: List[str] = []
                    for inst in top_candidates:
                        vt = self._instrument_to_vt(str(inst))
                        if vt:
                            candidate_vts.append(vt)
                    self._refresh_market_data_for_day(day, candidates=candidate_vts)
                    rebal_stats = self.rebalance_to_target(prev_day_pred_score, on_day=day)
                    self.write_log(
                        f"[replay] day {i+1}/{total} {day_iso} rebalance: "
                        f"sells={rebal_stats['sells_dispatched']} buys={rebal_stats['buys_dispatched']}"
                    )
                except Exception as exc:
                    self.write_log(
                        f"[replay] day {day_iso} rebalance 异常 {type(exc).__name__}: {exc}"
                    )

            # 2. 读今日推理结果 → 选 topk persist → 暂存全量 pred_score 给 day+1 决策
            try:
                today_pred_score = self._replay_apply_day(day, i + 1, total)
                if today_pred_score is not None and not today_pred_score.empty:
                    prev_day_pred_score = today_pred_score
            except Exception as exc:
                self.write_log(
                    f"[replay] day {i+1}/{total} {day_iso}: apply 异常 "
                    f"{type(exc).__name__}: {exc} (continuing)"
                )

            # 3. 日终结算：今日新买入转 yd_volume，下一交易日开盘可卖
            if gateway is not None:
                try:
                    gateway.td.counter.settle_end_of_day(day)
                except Exception as exc:
                    self.write_log(f"[replay] day {day_iso} settle 失败: {exc}")

            # 4. 写权益快照到本地 replay_history.db (A1/B2 解耦后的新路径,
            # ts=回放逻辑日 15:00). mlearnweb 端 replay_equity_sync_service
            # 通过 vnpy_webtrader endpoint 增量 fanout 拉.
            if gateway is not None:
                self._persist_replay_equity_snapshot(day, gateway)

            self.replay_progress = f"{i+1}/{total}"
            self.replay_last_done = day_iso

    def _persist_replay_equity_snapshot(self, day: date, gateway: Any) -> None:
        """从 gateway 当前 cash + 持仓市值算出"按回放日"的权益值,写入**本地**
        replay_history.db (A1/B2 解耦后的新路径).

        mlearnweb 端 replay_equity_sync_service 通过 vnpy_webtrader endpoint
        增量 fanout 拉, UPSERT 到 mlearnweb.db.strategy_equity_snapshots
        (source_label=replay_settle).

        与 mlearnweb 端 _resolve_strategy_value 算法一致:
            equity = cash + sum_over_positions(volume × cost_price + pnl)
        """
        try:
            from datetime import datetime as _dt, time as _time
            from .replay_history import write_snapshot

            counter = gateway.td.counter
            cash = float(counter.capital - counter.frozen)
            market_value = 0.0
            n_positions = 0
            for pos in counter.positions.values():
                vol = float(getattr(pos, "volume", 0) or 0)
                if vol <= 0:
                    continue
                price = float(getattr(pos, "price", 0) or 0)
                pnl = float(getattr(pos, "pnl", 0) or 0)
                market_value += vol * price + pnl
                n_positions += 1
            equity = cash + market_value

            # ts: 当日 15:00（A 股收盘）
            ts = _dt.combine(day, _time(hour=15, minute=0, second=0))
            ok = write_snapshot(
                strategy_name=self.strategy_name,
                ts=ts,
                strategy_value=equity,
                account_equity=equity,
                positions_count=n_positions,
                raw_variables={
                    "replay_status": getattr(self, "replay_status", ""),
                    "replay_progress": getattr(self, "replay_progress", ""),
                },
            )
            if not ok:
                return
            # 首日成功写入时 log 一条便于 user 确认链路通
            if not getattr(self, "_replay_persist_logged_first", False):
                self.write_log(
                    f"[replay] 本地 replay_history.db 权益快照已开始写入 "
                    f"(day={day} equity={equity:.0f}); mlearnweb 端 "
                    f"replay_equity_sync_service 会按 5min 周期 fanout 拉"
                )
                self._replay_persist_logged_first = True
        except Exception as exc:
            self.write_log(f"[replay] day {day} 权益快照写入失败: {type(exc).__name__}: {exc}")

    def _refresh_market_data_for_day(
        self,
        day: date,
        candidates: Optional[Iterable[str]] = None,
    ) -> None:
        """让 gateway.md 用 day 的参考价（reference_kind=today_open 时为当日 open）
        刷新所有**当前持仓 + 新候选股**的 tick，供 rebalance 拿参考价 / 撮合时取成交价。

        关键：批量回放期间 md 默认按 datetime.now() 取价，但回放是过去日期。
        显式调 md.refresh_tick(vt, as_of_date=day) 把 tick 价更新为 day 的参考价。

        candidates 必须包含**新买入候选**的 vt_symbol（不在当前持仓里的）；
        否则它们的 tick 还是 vnpy 启动时 set_synthetic_tick 兜底的值，
        会污染 _get_reference_price 与 _resolve_trade_price 两条读价路径。
        """
        gateway = self._get_own_gateway()
        if gateway is None:
            return
        md = getattr(gateway, "md", None)
        if md is None or not hasattr(md, "refresh_tick"):
            return
        symbols = set(self._get_long_positions().keys())
        if candidates:
            symbols.update(c for c in candidates if c)
        for vt in symbols:
            try:
                md.refresh_tick(vt, as_of_date=day)
            except Exception:
                pass

    def _need_batch_predict(self, days: List[date]) -> bool:
        """检查是否需要重跑批量推理：任一交易日缺 batch_mode diagnostics 就返 True。

        续跑幂等支持：上次跑过的批量结果若覆盖本次窗口全部交易日，可跳过推理直接 apply。
        """
        if not self.replay_skip_existing:
            return True
        for day in days:
            day_str = day.strftime("%Y%m%d")
            diag_path = Path(self.output_root) / self.strategy_name / day_str / "diagnostics.json"
            if not diag_path.exists():
                return True
            try:
                diag = json.loads(diag_path.read_text(encoding="utf-8"))
                if not diag.get("batch_mode"):
                    # 单日模式遗留 / 早期失败的 diagnostics：重跑批量覆盖
                    return True
                if diag.get("status") not in ("ok", "empty"):
                    return True
            except Exception:
                return True
        return False

    def _replay_apply_day(self, day: date, day_idx: int, total: int) -> Optional[pd.Series]:
        """逐日 apply：读已写的 predictions.parquet → select_topk persist → 返回全量 pred_score。

        Phase 6: 返回值从 selected (head topk DataFrame) 改为全量 pred_score (Series),
        供下一交易日 qlib TopkDropoutStrategy 算法决策用（算法需要看完整候选池）。
        topk persist (selections.parquet) 仍然写，用于前端 LatestTopkCard 展示。

        empty / 无数据返回 None。
        """
        day_str = day.strftime("%Y%m%d")
        day_iso = day.strftime("%Y-%m-%d")
        day_dir = Path(self.output_root) / self.strategy_name / day_str

        diag_path = day_dir / "diagnostics.json"
        if not diag_path.exists():
            self.write_log(f"[replay] day {day_idx}/{total} {day_iso}: 无 diagnostics（推理未覆盖）")
            return None
        try:
            diag = json.loads(diag_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.write_log(f"[replay] day {day_idx}/{total} {day_iso}: 读 diag 失败 {exc}")
            return None

        status = diag.get("status")
        if status == "empty":
            self.write_log(f"[replay] day {day_idx}/{total} {day_iso}: empty (qlib 数据未覆盖)")
            return None
        if status not in ("ok", "completed"):
            self.write_log(f"[replay] day {day_idx}/{total} {day_iso}: status={status!r} skip apply")
            return None

        pred_path = day_dir / "predictions.parquet"
        if not pred_path.exists():
            self.write_log(f"[replay] day {day_idx}/{total} {day_iso}: predictions.parquet 缺失")
            return None
        pred_df = pd.read_parquet(pred_path)

        # select_topk + persist（不再调 generate_orders — 由次日 rebalance 接管）
        selected = self.select_topk(pred_df)
        n_sel = 0 if selected is None else len(selected)
        try:
            self.persist_selections(selected, as_of_date=day)
        except Exception as exc:
            self.write_log(f"[replay] day {day_iso} persist_selections 失败: {exc}")

        # A1/B2 解耦: 不再直接写 mlearnweb.db.ml_metric_snapshots / ml_prediction_daily
        # 这两张表的数据流改成: vnpy 主进程发 EVENT_ML_METRICS / 暴露 vnpy_webtrader
        # /api/v1/ml/strategies/{name}/metrics?days=30 endpoint → mlearnweb 端
        # ml_snapshot_loop + historical_metrics_sync_service 拉取并 UPSERT 本地.
        # 详见 docs/deployment_a1_p21_plan.md §一. 步骤 1.

        self.write_log(
            f"[replay] day {day_idx}/{total} {day_iso}: ok rows={diag.get('rows', 0)} "
            f"topk={n_sel} → 暂存为下一交易日目标持仓"
        )

        # 更新 last_* 状态变量（与 run_daily_pipeline 一致，让 mlearnweb / UI 看到进展）
        self.last_run_date = day_iso
        self.last_status = "ok"
        self.last_n_pred = int(diag.get("rows", 0))
        self.last_model_run_id = diag.get("model_run_id", "") or ""

        # 返回全量 pred_score (Series): 取 pred_df 当日的 score 列，
        # index = instrument (ts_code), value = score
        try:
            last_dt = pred_df.index.get_level_values("datetime").max()
            today_pred = pred_df.xs(last_dt, level="datetime")
            # pred_df 通常只有 1 列 score
            if isinstance(today_pred, pd.DataFrame):
                pred_score = today_pred.iloc[:, 0]
            else:
                pred_score = today_pred
            return pred_score
        except Exception as exc:
            self.write_log(f"[replay] day {day_iso} 提取 pred_score 失败: {exc}")
            return None

    def _get_own_gateway(self) -> Optional[Any]:
        """从 main_engine 拿本策略的 gateway 实例（用于回放 enable_auto_settle 控制）。"""
        try:
            main_engine = getattr(self.signal_engine, "main_engine", None)
            if main_engine is None:
                return None
            return main_engine.get_gateway(self.gateway)
        except Exception:
            return None

    def _check_replay_day_outcome(self, day_str: str) -> Tuple[str, str]:
        """读 diagnostics.json 真实判定回放当日推理是否成功。

        返回 (status, error_message)。优先 diagnostics.json，缺失则退化到 self.last_*。
        """
        diag_path = Path(self.output_root) / self.strategy_name / day_str / "diagnostics.json"
        if diag_path.exists():
            try:
                diag = json.loads(diag_path.read_text(encoding="utf-8"))
                return (
                    str(diag.get("status", "")),
                    str(diag.get("error_message") or diag.get("error", "")),
                )
            except Exception as exc:
                return ("read_diag_failed", str(exc))
        return (str(self.last_status or ""), str(self.last_error or ""))
