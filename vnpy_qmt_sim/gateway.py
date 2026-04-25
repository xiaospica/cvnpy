# -*- coding: utf-8 -*-
from typing import Dict, Any
from datetime import datetime
from vnpy.event import EventEngine, Event, EVENT_TIMER
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    SubscribeRequest,
    OrderRequest,
    CancelRequest,
    AccountData,
    PositionData
)
from vnpy.trader.constant import Exchange, Status

from .bar_source import build_bar_source
from .md import QmtSimMd
from .td import QmtSimTd


class QmtSimGateway(BaseGateway):
    """
    QMT模拟网关
    """

    default_name = "QMT_SIM"

    default_setting: Dict[str, Any] = {
        # "账户" 不写默认字面量；connect() 会把 gateway_name 作为 fallback 注入，
        # 多 gateway 实例并存时天然有不同 account_id（独立 SQLite 文件 + 独立持仓/账户）。
        "模拟资金": 10000000.0,
        "部分成交率": 0.0,
        "拒单率": 0.0,
        "订单超时秒数": 30,
        "成交延迟毫秒": 0,
        "报单上报延迟毫秒": 0,
        "卖出持仓不足拒单": "是",
        "行情源": "merged_parquet",
        "merged_parquet_merged_root": r"D:\vnpy_data\snapshots\merged",
        "merged_parquet_reference_kind": "prev_close",
        "merged_parquet_fallback_days": 10,
        "merged_parquet_stale_warn_hours": 48,
        "启用持久化": "是",
        "持久化目录": r"F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state",
    }

    exchanges = [Exchange.SSE, Exchange.SZSE]

    def __init__(self, event_engine: EventEngine, gateway_name: str):
        super().__init__(event_engine, gateway_name)

        self.md = QmtSimMd(self)
        self.td = QmtSimTd(self)
        self._timer_count = 0
        self._order_timeout_interval = 1
        self.connected = False
        # 上次 timer tick 看到的日期，用于检测自然日切换触发 settle_end_of_day
        self._last_seen_date = None

    def connect(self, setting: dict):
        """连接行情与交易模块，并注册超时检查定时任务。"""
        # 默认 account_id = gateway_name：避免多 QmtSimGateway 实例共用默认账户名
        # 而冲突写入同一个 SQLite 文件（同机多策略沙盒隔离的前置条件）。
        setting = dict(setting)
        setting.setdefault("账户", self.gateway_name)

        self._build_bar_source(setting)
        self.md.connect()
        self.td.connect(setting)
        self.td.counter.order_timeout = int(setting.get("订单超时秒数", 30))
        self.td.counter.fill_delay_ms = int(setting.get("成交延迟毫秒", 0))
        self.td.counter.reporting_delay_ms = int(setting.get("报单上报延迟毫秒", 0))
        self.td.counter.reject_short_if_no_position = str(setting.get("卖出持仓不足拒单", "是")) == "是"

        self._setup_persistence(setting)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

        self.write_log("模拟网关连接成功")
        self.connected = True

    def _setup_persistence(self, setting: dict) -> None:
        """启用持久化时构造 QmtSimPersistence 并恢复账户/持仓/订单状态。

        恢复策略（GFD 规则）：
        - 账户 capital 恢复，frozen 重置为 0（活跃订单将被 cancel）
        - 持仓 volume/yd_volume/price 恢复，frozen 重置为 0
        - 活跃订单（SUBMITTING/NOTTRADED/PARTTRADED）→ CANCELLED 并落库
        """
        if str(setting.get("启用持久化", "是")) != "是":
            return
        try:
            from .persistence import QmtSimPersistence
        except Exception as exc:
            self.write_log(f"持久化模块加载失败，跳过: {exc}")
            return

        root = setting.get("持久化目录") or r"F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state"
        account_id = setting.get("账户", "test_id")
        try:
            persistence = QmtSimPersistence(account_id=str(account_id), root=root)
        except Exception as exc:
            self.write_log(f"持久化层初始化失败: {exc}")
            return

        self.td.counter.attach_persistence(persistence)

        try:
            state = persistence.restore(gateway_name=self.gateway_name)
        except Exception as exc:
            self.write_log(f"持久化恢复失败: {exc}")
            return

        if state.capital > 0:
            self.td.counter.capital = state.capital
            self.td.counter.frozen = 0.0
            for pos in state.positions:
                pos_key = f"{pos.vt_symbol}.{pos.direction.value}"
                self.td.counter.positions[pos_key] = pos
                self.td.counter._emit_position(pos)
            self.td.counter.push_account()
            self.write_log(
                f"持久化恢复: capital={state.capital:.2f} positions={len(state.positions)} "
                f"cancelled_orders={len(state.cancelled_active_orders)}"
            )

    def _build_bar_source(self, setting: dict) -> None:
        """按 setting["行情源"] 构建 bar_source 并注入到 md。

        约定：以 f"{source_name}_" 前缀的 setting 键自动剥前缀后透传给 source 构造器。
        """
        source_name = str(setting.get("行情源", "")).strip()
        if not source_name:
            self.write_log("未配置行情源，使用合成 tick")
            return
        prefix = f"{source_name}_"
        kwargs = {k[len(prefix):]: v for k, v in setting.items() if k.startswith(prefix)}
        try:
            self.md.source = build_bar_source(source_name, **kwargs)
            self.write_log(f"行情源装配成功: {source_name} {kwargs}")
        except Exception as exc:
            self.md.source = None
            self.write_log(f"行情源装配失败 ({source_name}): {exc}，使用合成 tick")

    def subscribe(self, req: SubscribeRequest):
        self.md.subscribe(req)

    def get_full_tick(self, vt_symbol: str):
        if hasattr(self.md, "get_full_tick"):
            return self.md.get_full_tick(vt_symbol)
        return None

    def send_order(self, req: OrderRequest) -> str:
        return self.td.send_order(req)

    def cancel_order(self, req: CancelRequest):
        self.td.cancel_order(req)

    def query_account(self):
        """查询账户"""
        self.td.query_account()

    def query_position(self):
        """查询持仓"""
        self.td.query_position()
        
    def query_orders(self):
        """查询委托"""
        self.td.query_orders()
        
    def query_trades(self):
        """查询成交"""
        self.td.query_trades()

    def close(self):
        try:
            self.event_engine.unregister(EVENT_TIMER, self.process_timer_event)
        except Exception:
            pass

    def process_timer_event(self, event: Event) -> None:
        """事件循环入口，按周期触发超时订单扫描，并在自然日切换时触发日终结算。"""
        self._timer_count += 1
        now = datetime.now()
        today = now.date()

        if self._last_seen_date is None:
            self._last_seen_date = today
        elif today > self._last_seen_date:
            try:
                self.td.counter.settle_end_of_day(self._last_seen_date)
            except Exception as exc:
                self.write_log(f"日终结算失败: {exc}")
            self._last_seen_date = today

        try:
            self.td.counter.process_simulation(now)
        except Exception:
            pass

        if self._timer_count % self._order_timeout_interval == 0:
            self.check_order_timeout()

    def check_order_timeout(self) -> None:
        """扫描超时活动订单并执行撤单与冻结释放。"""
        now = datetime.now()
        timeout_orders = []
        for orderid, submit_time in list[tuple[str, datetime]](self.td.counter.order_submit_time.items()):
            order = self.td.counter.orders.get(orderid)
            timeout_seconds = self.td.counter.order_timeout
            if order:
                extra = getattr(order, "extra", None)
                if isinstance(extra, dict):
                    try:
                        timeout_seconds = int(extra.get("timeout_seconds") or timeout_seconds)
                    except Exception:
                        timeout_seconds = self.td.counter.order_timeout

            if (now - submit_time).total_seconds() <= timeout_seconds:
                continue
            if not order:
                self.td.counter.order_submit_time.pop(orderid, None)
                continue
            if order.traded >= order.volume:
                self.td.counter.order_submit_time.pop(orderid, None)
                continue
            if order.status in [Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED]:
                timeout_orders.append(order)

        for order in timeout_orders:
            self.td.counter.release_order_frozen_cash(order.orderid)
            order.status = Status.CANCELLED
            self.td.counter.order_submit_time.pop(order.orderid, None)
            self.td.counter.order_reject_reason.pop(order.orderid, None)
            self.on_order(order)
            self.write_log(f"订单超时自动撤单: {order.orderid}")
