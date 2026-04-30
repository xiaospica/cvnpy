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
from typing import Any, Dict, List, Optional, Tuple

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
        "cash_per_order",
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
    trigger_time: str = "21:00"  # 20:00 拉数后 1h, live_end=today 数据已齐
    buy_sell_time: str = "09:30"  # 实盘 T+1 开盘交易时间（cron 注册见后续实盘版本）
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

            if self.enable_trading:
                self.last_stage = Stage.ORDER.value
                self.write_log(f"generate_orders enabled, dispatching {len(selected)} orders")
                self.generate_orders(selected)
            else:
                self.write_log("enable_trading=False, skip generate_orders (dry-run)")

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
        sel_df[COL_WEIGHT] = 1.0 / len(sel_df)
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

    def generate_orders(self, selected: pd.DataFrame) -> None:
        """子类实现. 内部调 self.send_order(...). 仅在 enable_trading=True 时被调用."""
        raise NotImplementedError("subclass should implement generate_orders")

    # -----------------------------------------------------------------
    # 调仓 — diff: sells (持仓 - 信号) + buys (信号 - 持仓)
    # -----------------------------------------------------------------
    # 实盘模式：T 日 21:00 推理产出 topk → T+1 日 09:30 调 rebalance_to_target
    # 回放模式：T 日 在 _replay_apply_day 暂存 topk → T+1 日开头调 rebalance（详见 _run_replay_loop）

    def rebalance_to_target(
        self,
        target_topk: pd.DataFrame,
        on_day: Optional[date] = None,
    ) -> Dict[str, Any]:
        """根据目标 topk + 当前持仓做 diff，先卖后买。

        Parameters
        ----------
        target_topk : DataFrame
            索引含 ``instrument`` (如 "000001.SZ") 的目标持仓表。空 DataFrame 表示
            清空所有持仓（全部 sell）。
        on_day : datetime.date, optional
            执行交易的逻辑日。回放传当日；实时不传走 today。用于查询当日参考价。

        Returns
        -------
        dict: {sells_dispatched, buys_dispatched, sells_skipped, buys_skipped}

        语义：
            sells = 当前持仓 - target_topk     → 全卖（按 yd_volume，T+1 限制）
            buys  = target_topk - 当前持仓     → 按 cash_per_order ÷ 当日 pre_close 算手数
            keeps = 交集                        → 不动
        """
        on_day = on_day or date.today()
        stats = {"sells_dispatched": 0, "buys_dispatched": 0, "sells_skipped": 0, "buys_skipped": 0}

        if target_topk is None:
            target_topk = pd.DataFrame()
        # vt_symbol 集合
        target_set = set()
        for inst in (target_topk.index if not target_topk.empty else []):
            vt = self._instrument_to_vt(str(inst))
            if vt:
                target_set.add(vt)

        positions = self._get_long_positions()  # dict: vt_symbol → PositionData
        current_set = set(positions.keys())

        sells = current_set - target_set
        buys = target_set - current_set

        self.write_log(
            f"[rebalance] on_day={on_day} target={len(target_set)} current={len(current_set)} "
            f"→ sells={len(sells)} buys={len(buys)} keeps={len(current_set & target_set)}"
        )

        # 1. sells（先卖释放资金）
        for vt in sells:
            pos = positions.get(vt)
            if pos is None:
                stats["sells_skipped"] += 1
                continue
            sell_volume = float(getattr(pos, "yd_volume", 0) or 0)
            if sell_volume <= 0:
                # 当日新买，T+1 不可卖（或没 yd 仓位）
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

        # 2. buys（后买）
        for vt in buys:
            ref_price = self._get_reference_price(vt)
            if ref_price is None or ref_price <= 0:
                stats["buys_skipped"] += 1
                self.write_log(f"[rebalance] skip buy {vt}: 无参考价")
                continue
            volume = self._compute_buy_volume(ref_price)
            if volume <= 0:
                stats["buys_skipped"] += 1
                self.write_log(
                    f"[rebalance] skip buy {vt}: cash_per_order={self.cash_per_order} "
                    f"price={ref_price} → volume={volume}"
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
                if str(getattr(pos, "direction", "")) != Direction.LONG.value:
                    continue
                if float(getattr(pos, "volume", 0) or 0) <= 0:
                    continue
                positions[pos.vt_symbol] = pos
        except Exception as exc:
            self.write_log(f"_get_long_positions 失败: {exc}")
        return positions

    def _get_reference_price(self, vt_symbol: str) -> Optional[float]:
        """拿当日参考价（默认 pre_close / last_price）用于算买入手数。

        实盘：从 main_engine 拿当日 tick.last_price。
        回放：md.get_quote(vt_symbol) 已被 _replay_loop 显式 refresh 到当日 pre_close。
        """
        try:
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

    def _compute_buy_volume(self, ref_price: float) -> int:
        """按 cash_per_order ÷ 价格 算手数，向下取整 100 股（A 股最小买入单位）。"""
        if ref_price <= 0:
            return 0
        raw = float(self.cash_per_order) / float(ref_price)
        lots = int(raw // 100)
        return max(0, lots * 100)

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
        # Phase 4：仅 sim 模式策略启动后台线程跑回放（不阻塞 on_start 返回）
        try:
            self._start_replay_if_needed()
        except Exception as exc:
            # 回放启动失败不影响策略实时模式；记日志即可
            self.write_log(f"_start_replay_if_needed failed: {exc}")

    def on_stop(self) -> None:
        self.trading = False
        self.signal_engine.unregister_daily_job(strategy_name=self.strategy_name)
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
        """后台线程跑回放循环。详见 plan 文档 Phase 4 边界控制。"""
        scheduler = getattr(self.signal_engine, "scheduler", None)
        gateway = self._get_own_gateway()

        # 1. 暂停本策略 APScheduler cron（仅按 strategy_name 隔离，不影响其他策略）
        if scheduler is not None:
            try:
                scheduler.pause_job(self.strategy_name)
                self.write_log(f"[replay] 暂停 cron job ({self.strategy_name})")
            except Exception as exc:
                self.write_log(f"[replay] pause_job 失败 (continuing): {exc}")

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
            # 5. 恢复本策略 cron
            if scheduler is not None:
                try:
                    scheduler.resume_job(self.strategy_name)
                    self.write_log(f"[replay] 恢复 cron job ({self.strategy_name})")
                except Exception as exc:
                    self.write_log(f"[replay] resume_job 失败: {exc}")

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
        # 回放映射：day[i-1] 推理产出的 topk = day[i] 的目标持仓
        #   for day in days:
        #     1. 用 prev_day_topk 在 day 开盘做 rebalance（先卖后买，cash_per_order ÷ pre_close）
        #     2. 读 day 的 predictions.parquet → select_topk → 暂存为 day+1 的目标
        #     3. 显式 settle_end_of_day(day)：今日买入转 yd 给 day+1 卖出
        prev_day_topk: Optional[pd.DataFrame] = None

        for i, day in enumerate(days):
            day_str = day.strftime("%Y%m%d")
            day_iso = day.strftime("%Y-%m-%d")

            # 1. 用上一交易日的 topk 在今日开盘 rebalance
            if prev_day_topk is not None and self.enable_trading:
                try:
                    self._refresh_market_data_for_day(day)
                    rebal_stats = self.rebalance_to_target(prev_day_topk, on_day=day)
                    self.write_log(
                        f"[replay] day {i+1}/{total} {day_iso} rebalance: "
                        f"sells={rebal_stats['sells_dispatched']} buys={rebal_stats['buys_dispatched']}"
                    )
                except Exception as exc:
                    self.write_log(
                        f"[replay] day {day_iso} rebalance 异常 {type(exc).__name__}: {exc}"
                    )

            # 2. 读今日推理结果 → 选 topk → 暂存为下一交易日目标 + 写 selections.parquet
            try:
                today_topk = self._replay_apply_day(day, i + 1, total)
                if today_topk is not None and not today_topk.empty:
                    prev_day_topk = today_topk
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

            self.replay_progress = f"{i+1}/{total}"
            self.replay_last_done = day_iso

    def _refresh_market_data_for_day(self, day: date) -> None:
        """让 gateway.md 用 day 的 pre_close 刷新所有当前持仓 + 候选股 tick，供 rebalance 拿参考价。

        关键：批量回放期间 md 默认按 datetime.now() 取价，但回放是过去日期。
        显式调 md.refresh_tick(vt, as_of_date=day) 把 tick 价更新为 day 的 pre_close。
        """
        gateway = self._get_own_gateway()
        if gateway is None:
            return
        md = getattr(gateway, "md", None)
        if md is None or not hasattr(md, "refresh_tick"):
            return
        # 当前持仓 + prev topk 的所有 vt_symbol 都需要刷
        symbols = set()
        for vt in self._get_long_positions().keys():
            symbols.add(vt)
        # 刷一次足够 — rebalance 只读这些 vt 的 tick
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

    def _replay_apply_day(self, day: date, day_idx: int, total: int) -> Optional[pd.DataFrame]:
        """逐日 apply：读已写的 predictions.parquet → select_topk → persist_selections。

        返回当日 topk DataFrame（供下一交易日 rebalance 用）。empty / 无数据返回 None。

        注意：本方法**不再直接调 generate_orders**。下单由 _replay_loop_body 在
        次日开始时调 rebalance_to_target(prev_day_topk, on_day=current_day) 完成，
        符合"T 日推理 → T+1 日开盘交易"的真实策略时序。
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

        self.write_log(
            f"[replay] day {day_idx}/{total} {day_iso}: ok rows={diag.get('rows', 0)} "
            f"topk={n_sel} → 暂存为下一交易日目标持仓"
        )

        # 更新 last_* 状态变量（与 run_daily_pipeline 一致，让 mlearnweb / UI 看到进展）
        self.last_run_date = day_iso
        self.last_status = "ok"
        self.last_n_pred = int(diag.get("rows", 0))
        self.last_model_run_id = diag.get("model_run_id", "") or ""

        return selected

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
