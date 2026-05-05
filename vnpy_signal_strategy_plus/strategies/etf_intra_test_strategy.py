# -*- coding: utf-8 -*-
"""端到端回归测试用的 MySQL 信号策略 + 回放控制器。

与生产策略（如 ``MultiStrategySignalStrategyPlus``）的差异：

1. ``strategy_name = "etf_intra_test"``：与 Redis stream key、bridge
   target_stg、MySQL ``stock_trade.stg`` 三处必须保持一致。
2. ``load_external_setting()`` 从 ``test/test_setting.json`` 读 mysql 段，
   不动生产 ``mysql_signal_setting.json``。
3. ``run_polling()`` **重写为回放控制器**：按 ``stock_trade.remark`` 升序
   遍历未处理信号，跨日触发 sim 网关 ``settle_end_of_day``，让 T+1 SELL
   能在第 N 日卖出第 N-1 日及之前买入的"昨仓"，资金循环回流，避免
   "全 BUY 占满资金 + 全 SELL 拒"的退化模式。

关键回放钩子（vnpy_qmt_sim 已支持，详见 td.py:65、gateway.py:222）：

- ``gateway.enable_auto_settle(False)``：禁用网关 timer 自动 settle，
  改由策略层显式驱动；
- ``md.refresh_tick(vt, as_of_date=day)``：把 tick.last_price 刷成当日
  bar.open（reference_kind=today_open），让 process_signal 仓位计算和
  sim 撮合都按"该日"价格；
- ``td.counter._replay_now = sig.remark``：trade.datetime / order.datetime
  使用回放时间，而非 datetime.now()；
- ``td.counter.settle_end_of_day(day)``：把 ``volume`` 复制给 ``yd_volume``
  做 T+1 结转，并按 pct_chg / close/open 做 mark-to-market。

⚠️ ``test/test_setting.json`` 必须设 ``replay.rebase_remark_to_today=false``，
让信号保留原始 CSV 的历史日期，否则跨日 settle 不会被触发。
"""
from __future__ import annotations

import json
import re
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from vnpy.trader.constant import Direction, Exchange
from vnpy.trader.object import PositionData

from vnpy_signal_strategy_plus.mysql_signal_strategy import (
    MySQLSignalStrategyPlus,
    Stock,
)
from vnpy_signal_strategy_plus.utils import convert_code_to_vnpy_type

# 4/14 快照初始化逻辑用：从 instrument(`名元股份(003003.XSHE)`) 提取代码
_INSTRUMENT_RE = re.compile(r"\((\d{6}\.[A-Z]+)\)")
_JQ_TO_VNPY = {"XSHG": "SSE", "XSHE": "SZSE"}

# 测试配置文件路径：strategies/etf_intra_test_strategy.py
#   .parent       = strategies/
#   .parent.parent = vnpy_signal_strategy_plus/
# 优先用 test_setting.local.json（含真实密码、加 .gitignore），fallback 到模板。
def _resolve_setting_path(template_path: Path) -> Path:
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


TEST_SETTING_PATH = _resolve_setting_path(
    Path(__file__).resolve().parent.parent / "test" / "test_setting.json"
)


class EtfIntraTestStrategy(MySQLSignalStrategyPlus):
    """E2E 测试专用策略类，仅用于回归 Redis -> MySQL -> sim 链路。"""

    author = "e2e-test"
    strategy_name = "etf_intra_test"

    def load_external_setting(self) -> None:
        """从 test_setting.json 加载 mysql 连接 + 测试期参数。

        覆盖父类 ``load_external_setting``——父类读 mysql_signal_setting.json，
        测试场景下不希望污染生产配置文件。
        """
        if not TEST_SETTING_PATH.exists():
            self.write_log(f"[etf-test] 未找到测试配置: {TEST_SETTING_PATH}")
            return

        try:
            with open(TEST_SETTING_PATH, "r", encoding="utf-8") as f:
                setting = json.load(f)
        except Exception as exc:
            self.write_log(f"[etf-test] 测试配置读取失败: {exc}")
            return

        try:
            mysql_cfg = setting["mysql"]
            self.db_host = mysql_cfg["host"]
            self.db_port = int(mysql_cfg["port"])
            self.db_user = mysql_cfg["user"]
            self.db_password = mysql_cfg["password"]
            self.db_name = mysql_cfg["db"]
        except KeyError as exc:
            self.write_log(f"[etf-test] 测试配置缺少 mysql 字段: {exc}")
            return

        self.poll_interval = 0.05
        # 实盘模式：strategy 收到 stock_trade 中 processed=False 的当天信号即下单。
        # 注意 mysql_signal_strategy.run_polling 会把"当天"判定为
        # ``self.current_dt = datetime.now(CHINA_TZ)`` 起点，所以测试时
        # 注入的信号 remark 时间需落在今天（编排器会做时间 rebase）。
        self.engine_type = "实盘"
        # 占位日期，实盘模式下 strategy.on_start 用 datetime.now() 覆盖
        self.start_date = "20190101 00:00:00"
        self.end_date = "20300101 00:00:00"
        # 网关名：必须与 run_sim.py 中 add_gateway(QmtSimGateway, gateway_name=...)
        # 一致（默认 QMT_SIM）。
        self.gateway = setting.get("sim", {}).get("account_id", "QMT_SIM")

        replay_cfg = setting.get("replay", {}) or {}
        self._idle_settle_seconds = float(replay_cfg.get("idle_settle_seconds", 30))
        self._replay_enabled: bool = not bool(
            replay_cfg.get("rebase_remark_to_today", False)
        )
        # 持仓引导：date_range[0] 是回放起点，需要把 date_range[0] 前一日的
        # CSV 持仓快照注入 sim，让回放只跑增量（避免全期累计 fallback 价误差）。
        self._date_range_start: Optional[str] = None
        date_range = replay_cfg.get("date_range")
        if isinstance(date_range, list) and len(date_range) == 2 and date_range[0]:
            self._date_range_start = str(date_range[0])
        self._csv_position_path: Optional[Path] = None
        self._csv_encoding: str = "gbk"
        csv_cfg = setting.get("csv", {}) or {}
        if csv_cfg.get("position_path"):
            self._csv_position_path = Path(csv_cfg["position_path"])
            self._csv_encoding = csv_cfg.get("encoding", "gbk")

        password_mask = "***" if self.db_password else ""
        self.write_log(
            f"[etf-test] 配置生效 host={self.db_host}:{self.db_port} "
            f"db={self.db_name} user={self.db_user} pwd={password_mask} "
            f"gateway={self.gateway} replay_enabled={self._replay_enabled} "
            f"idle_settle={self._idle_settle_seconds}s"
        )

    # ------------------------------------------------------------------
    # 资金口径修正
    # ------------------------------------------------------------------

    def get_account_asset(self, gateway_name: str) -> float:
        """覆盖父类：返回总权益 = 现金 + 持仓市值，而非仅 balance（现金）。

        ⚠️ 关键修复：mysql_signal_strategy.process_signal 用 ``total_capital * pct``
        算目标股数，``total_capital`` 默认取自 OMS account.balance（即现金）。但
        CSV 的 pct = amt * price / equity_total，equity_total = 现金 + 持仓市值。

        seed 持仓引导后 sim 现金被扣到 ~2752 元（持仓占了 99.7w），如果策略用
        balance=2752 算 vol_int，所有信号 vol_int 都 < 100 股 → 全部"下单数量
        为 0"被跳过 → sim 一笔不下。

        修复：本类返回 balance + sum(pos.volume * pos.price)，与 CSV 的 equity_total
        口径对齐。生产场景下持仓 mark-to-market 后两者基本守恒，本 override 也不
        会让生产策略行为变化（前提是该策略只在 e2e 测试中加载）。
        """
        base = super().get_account_asset(gateway_name)
        sim_gw = self._get_sim_gateway()
        if sim_gw is None or gateway_name != self.gateway:
            return base
        try:
            positions_value = sum(
                float(p.volume) * float(p.price)
                for p in sim_gw.td.counter.positions.values()
                if float(p.volume) > 0
            )
        except Exception:
            positions_value = 0.0
        equity = base + positions_value
        if positions_value > 0:
            self.write_log(
                f"[equity] cash={base:,.0f} + positions_mv={positions_value:,.0f} "
                f"= equity={equity:,.0f}"
            )
        return equity

    # ------------------------------------------------------------------
    # 持仓引导（窄 date_range 回放专用）
    # ------------------------------------------------------------------

    def on_init(self) -> None:
        """覆盖父类：先 connect_db，再尝试注入"date_range[0] 前一日"的 csv 持仓。"""
        super().on_init()
        if not self._replay_enabled:
            return
        if self._date_range_start is None or self._csv_position_path is None:
            return
        if not self._csv_position_path.exists():
            self.write_log(
                f"[seed] csv_position_path 不存在，跳过持仓引导: {self._csv_position_path}"
            )
            return
        try:
            self._seed_initial_positions()
        except Exception as exc:
            self.write_log(
                f"[seed] 持仓引导异常（不阻塞）: {exc}\n{traceback.format_exc()}"
            )

    def _seed_initial_positions(self) -> None:
        """从 csv_position_path 读 date_range[0] 前一交易日的持仓快照，注入 sim 网关。

        语义：date_range=['2026-04-15', '2026-04-29'] 表示**只回放 4/15-4/29 的
        信号增量**；4/15 开盘前的持仓 = csv 的 4/14 收盘快照。把 csv 4/14 的持仓
        装载到 sim：positions / yd_volume / capital 都按 csv 当日数据初始化，
        让 sim 4/29 的累计 = csv 4/14 起始 + sim 增量回放 = csv 4/29 终态。
        """
        try:
            import pandas as pd
        except ImportError:
            self.write_log("[seed] pandas 未安装，跳过持仓引导")
            return

        cols = [
            "date", "category", "instrument", "direction", "volume", "available",
            "close", "market_value", "pnl", "open_price", "holding_price",
            "margin", "today_pnl", "today_volume", "pnl_ratio",
            "equity_total", "position_ratio",
        ]
        df = pd.read_csv(
            self._csv_position_path, encoding=self._csv_encoding, skiprows=1,
            header=None, names=cols, engine="python", on_bad_lines="warn",
        )
        df["date"] = df["date"].astype(str)
        df = df[df["category"].notna()].copy()
        # date_range[0] 之前的最后一个交易日
        prev_dates = sorted(df[df["date"] < self._date_range_start]["date"].unique())
        if not prev_dates:
            self.write_log(
                f"[seed] csv 中找不到 < {self._date_range_start} 的持仓快照，跳过引导"
            )
            return
        seed_date = prev_dates[-1]
        snap = df[df["date"] == seed_date]
        self.write_log(
            f"[seed] 用 {seed_date} 的快照引导 sim 持仓 (回放起点={self._date_range_start})"
        )

        sim_gw = self._get_sim_gateway()
        if sim_gw is None:
            self.write_log("[seed] sim 网关未就绪，跳过引导")
            return

        injected = 0
        used_cash = 0.0
        for _, r in snap.iterrows():
            m = _INSTRUMENT_RE.search(str(r["instrument"]))
            if not m:
                continue
            code, suffix = m.group(1).split(".")
            vnpy_suffix = _JQ_TO_VNPY.get(suffix)
            if not vnpy_suffix:
                continue
            try:
                volume = int(float(str(r["volume"]).rstrip("股").rstrip(",")))
                close = float(r["close"])
                mv = float(r["market_value"])
            except (TypeError, ValueError):
                continue
            if volume <= 0 or close <= 0:
                continue

            symbol_only = code
            exch = Exchange(vnpy_suffix)
            vt_symbol = f"{symbol_only}.{vnpy_suffix}"
            pos_key = f"{vt_symbol}.{Direction.LONG.value}"

            pos = PositionData(
                symbol=symbol_only,
                exchange=exch,
                direction=Direction.LONG,
                volume=float(volume),
                yd_volume=float(volume),  # 全部作为可卖昨仓
                frozen=0.0,
                price=close,
                gateway_name=sim_gw.gateway_name,
            )
            sim_gw.td.counter.positions[pos_key] = pos
            sim_gw.td.counter._emit_position(pos)
            used_cash += mv
            injected += 1

        # 扣减现金（持仓占用）
        sim_gw.td.counter.capital -= used_cash
        sim_gw.td.counter.push_account()

        # 关键：_emit_position 通过 EventEngine 异步推送 EVENT_POSITION 给 OMS。
        # 在 on_init 紧接 on_start 启动 poll_thread 时，OMS 队列还没消化完 →
        # process_signal 内 main_engine.get_all_positions() 拿不到刚注入的持仓 →
        # SELL 单全部 "未找到持仓" 静默 return → sim 一笔未下单。
        # 修复：等 EventEngine 队列排空后再返回。
        try:
            ee = self.signal_engine.event_engine
            queue = getattr(ee, "_queue", None)
            if queue is not None:
                deadline = time.time() + 5.0
                while queue.qsize() > 0 and time.time() < deadline:
                    time.sleep(0.05)
        except Exception:
            time.sleep(1.0)  # 兜底

        # 同步直接写 OMS positions 字典（双保险，避免事件丢失）
        try:
            oms = self.signal_engine.main_engine.engines.get("oms")
            if oms is not None and hasattr(oms, "positions"):
                for pos_key, pos in sim_gw.td.counter.positions.items():
                    oms.positions[pos.vt_positionid] = pos
                if hasattr(oms, "accounts") and sim_gw.td.counter.accounts:
                    for acc in sim_gw.td.counter.accounts.values():
                        oms.accounts[acc.vt_accountid] = acc
        except Exception as exc:
            self.write_log(f"[seed] 同步 OMS 异常（仅警告）: {exc}")

        self.write_log(
            f"[seed] 注入 {injected} 个持仓; 占用资金 {used_cash:,.0f}; "
            f"剩余 capital={sim_gw.td.counter.capital:,.0f}; OMS 已同步"
        )

    # ------------------------------------------------------------------
    # 回放控制器
    # ------------------------------------------------------------------

    def _get_sim_gateway(self):
        """从 main_engine 找到 sim 网关实例（同时校验拥有 td.counter / md / enable_auto_settle）。"""
        gw = self.signal_engine.main_engine.get_gateway(self.gateway)
        if gw is None:
            return None
        td = getattr(gw, "td", None)
        counter = getattr(td, "counter", None)
        md = getattr(gw, "md", None)
        if counter is None or md is None or not hasattr(gw, "enable_auto_settle"):
            return None
        return gw

    def _settle_day(self, sim_gw, day: date) -> None:
        """触发 sim 日终结算 + 写入回放权益快照（mlearnweb 前端权益曲线数据源）。"""
        try:
            sim_gw.td.counter.settle_end_of_day(day)
            self.write_log(
                f"[replay] settle_end_of_day({day}) yd 已结转，T+1 限制对此日及之前持仓解锁"
            )
        except Exception as exc:
            self.write_log(f"[replay] settle_end_of_day({day}) 异常: {exc}")
            return

        # 写回放逻辑日权益快照到 D:/vnpy_data/state/replay_history.db。mlearnweb
        # 端 replay_equity_sync_loop（每 5 分钟）从 vnpy_webtrader 的
        # /api/v1/ml/strategies/.../replay/equity_snapshots fanout 拉到自己的
        # strategy_equity_snapshots(source_label='replay_settle')，前端 curve 字段
        # 就是这些点。算法与 vnpy_ml_strategy._persist_replay_equity_snapshot 一致。
        try:
            from datetime import time as _time
            from vnpy_ml_strategy.replay_history import write_snapshot

            counter = sim_gw.td.counter
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
            ts = datetime.combine(day, _time(hour=15, minute=0, second=0))
            ok = write_snapshot(
                strategy_name=self.strategy_name,
                ts=ts,
                strategy_value=equity,
                account_equity=equity,
                positions_count=n_positions,
                raw_variables={"replay_day": str(day)},
            )
            if ok and not getattr(self, "_replay_persist_logged_first", False):
                self.write_log(
                    f"[replay] replay_history.db 权益快照已写入 (day={day} equity={equity:.0f})"
                    "; mlearnweb 后端 replay_equity_sync_loop ~5min fanout 同步"
                )
                self._replay_persist_logged_first = True
        except Exception as exc:
            self.write_log(f"[replay] day {day} 权益快照写入失败: {type(exc).__name__}: {exc}")

    def run_polling(self) -> None:
        """覆盖父类：按 remark 升序的"虚拟交易日"回放控制器。

        与父类的差异：

        - 不按 ``self.current_dt = datetime.now()`` 过滤"当天"信号；改为
          一次性拉所有未处理信号，按 ``remark`` 升序处理。
        - 每条信号前 ``refresh_tick(vt, as_of_date=sig_day)`` + 设
          ``_replay_now=sig.remark``，process_signal 内部 send_order
          产生的 trade 会带正确的回放日期与价格。
        - 跨日时（``sig_day > last_day``）先调 ``settle_end_of_day(last_day)``
          让 T+1 SELL 能在新一日卖出昨仓。
        - 全部信号消化后等待 ``idle_settle_seconds`` 静默期（让 bridge 写入
          完成），然后触发最后一日的 settle。

        ⚠️ 仅当 ``replay.rebase_remark_to_today=false`` 时启用回放语义；否则
        退化为父类行为（仍由 self._replay_enabled 守门）。
        """
        if not self._replay_enabled:
            self.write_log(
                "[replay] rebase_remark_to_today=true，回退到父类 run_polling（实时模式）"
            )
            return super().run_polling()

        sim_gw = self._get_sim_gateway()
        if sim_gw is None:
            self.write_log(
                f"[replay] 找不到 sim 网关 {self.gateway!r} 或缺少回放钩子，退回父类轮询"
            )
            return super().run_polling()

        sim_gw.enable_auto_settle(False)
        self.write_log(
            f"[replay] 回放模式启动 gateway={self.gateway}; "
            "auto_settle 已禁用，由本策略按 remark 推进 + 显式 settle"
        )

        last_day: Optional[date] = None
        last_signal_ts: float = time.time()

        while self.is_polling_avtive:
            if not self.Session:
                time.sleep(self.poll_interval)
                continue

            try:
                session = self.Session()
                signals = (
                    session.query(Stock)
                    .filter(
                        Stock.stg == self.strategy_name,
                        Stock.processed == False,  # noqa: E712
                    )
                    .order_by(Stock.remark.asc(), Stock.id.asc())
                    .limit(50)
                    .all()
                )

                if not signals:
                    # 静默期到了 → 触发最后一日的 settle，关闭"未结转昨仓"
                    if (
                        last_day is not None
                        and (time.time() - last_signal_ts) >= self._idle_settle_seconds
                    ):
                        self._settle_day(sim_gw, last_day)
                        sim_gw.td.counter._replay_now = None
                        last_day = None  # 防止重复 settle
                    session.close()
                    time.sleep(max(self.poll_interval, 0.5))
                    continue

                last_signal_ts = time.time()

                for sig in signals:
                    if not self.is_polling_avtive:
                        break

                    sig_day = sig.remark.date() if isinstance(sig.remark, datetime) else sig.remark

                    # 跨日：先 settle 上一天（让昨仓滚动 → SELL 解锁）
                    if last_day is not None and sig_day > last_day:
                        self._settle_day(sim_gw, last_day)

                    # 每条信号都 refresh tick（merged_parquet 有缓存，重复调用便宜）
                    vt_symbol = convert_code_to_vnpy_type(sig.code)
                    try:
                        sim_gw.md.refresh_tick(vt_symbol, as_of_date=sig_day)
                    except Exception as exc:
                        self.write_log(
                            f"[replay] refresh_tick({vt_symbol}, {sig_day}) 异常: {exc}"
                        )

                    sim_gw.td.counter._replay_now = sig.remark
                    self.current_dt = sig.remark

                    try:
                        processed = self.process_signal(sig)
                    except Exception as exc:
                        self.write_log(
                            f"[replay] process_signal id={sig.id} 异常: {exc}\n"
                            f"{traceback.format_exc()}"
                        )
                        processed = False

                    if processed:
                        try:
                            sig.processed = True
                            session.commit()
                        except Exception as exc:
                            session.rollback()
                            self.write_log(
                                f"[replay] commit processed=True 失败 id={sig.id}: {exc}"
                            )
                    else:
                        session.rollback()

                    self.last_signal_id = max(self.last_signal_id, sig.id)
                    last_day = sig_day
                    # 让出 GIL 给 sim 撮合 + EventEngine 推送
                    time.sleep(0.01)

                session.close()
                self.put_event()

            except Exception as exc:
                self.write_log(
                    f"[replay] run_polling 异常: {exc}\n{traceback.format_exc()}"
                )
                try:
                    session.rollback()
                    session.close()
                except Exception:
                    pass
                time.sleep(1)

        # 退出时清 _replay_now（避免下一次启动用到 stale 时间戳）
        try:
            sim_gw.td.counter._replay_now = None
        except Exception:
            pass
