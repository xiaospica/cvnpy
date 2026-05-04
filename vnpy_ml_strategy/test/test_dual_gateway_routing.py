"""[P2-1.3 V1] 双 sim gateway 路由验证 — 不依赖实盘环境的多 gateway 架构核心.

目的: 用两个独立 sim gateway 替代 live + sim, 验证多 Gateway 架构中关键的
路由 / 隔离逻辑. 多 Gateway 路由与 gateway 类型无关 — sim+sim 跑通就证明
live+sim 也能跑通.

覆盖风险:
  R1 EventEngine 同时挂两个 Gateway 互不干扰 (各自独立 OnTick / OnTrade 通道)
  R2 send_order 路由正确: 策略 A 发到 gw_A, 不会跑到 gw_B
  R3 持仓 / 资金 SQLite 物理隔离 (sim_<gw_name>.db 文件)
  R4 命名 validator 双 gateway 各自校验通过

不在本测试覆盖 (留给 V2/V3):
  - signal_source_strategy 上游 selections 复用 (V2)
  - kind=live FakeQmtGateway 启动期校验 (V2)
  - 真 miniqmt RPC 链路 (V3 盘中)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from vnpy_qmt_sim import QmtSimGateway


def _setting(persist_root: Path, capital: float = 1_000_000.0) -> dict:
    return {
        "模拟资金": capital,
        "部分成交率": 0.0,
        "拒单率": 0.0,
        "订单超时秒数": 30,
        "成交延迟毫秒": 0,
        "报单上报延迟毫秒": 0,
        "卖出持仓不足拒单": "是",
        "行情源": "",  # 不依赖 bar_source
        "启用持久化": "是",
        "持久化目录": str(persist_root),
    }


def _buy(gw: QmtSimGateway, vt: str, vol: int, price: float, ref: str = "") -> str:
    sym, ex = vt.split(".")
    req = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=price, volume=vol, reference=ref,
    )
    return gw.send_order(req)


def test_dual_sim_gateway_db_isolated(tmp_path: Path) -> None:
    """R3: 双 sim gateway 持仓/资金 SQLite 物理隔离."""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_a")
        gw_a.connect(_setting(tmp_path, capital=1_000_000.0))
        gw_b = QmtSimGateway(ee, "QMT_SIM_b")
        gw_b.connect(_setting(tmp_path, capital=2_000_000.0))

        # 各自独立 db 文件 (默认 account_id = gateway_name)
        assert (tmp_path / "sim_QMT_SIM_a.db").exists()
        assert (tmp_path / "sim_QMT_SIM_b.db").exists()

        # 资金各自独立 (不共享 capital)
        assert gw_a.td.counter.capital == 1_000_000.0
        assert gw_b.td.counter.capital == 2_000_000.0
        # account_id 与 gateway_name 一致
        assert gw_a.td.counter.accountid == "QMT_SIM_a"
        assert gw_b.td.counter.accountid == "QMT_SIM_b"
    finally:
        ee.stop()


def test_send_order_routes_to_correct_gateway(tmp_path: Path) -> None:
    """R2: 策略 A 发到 gw_A, gw_B 不应有任何 trades / orders."""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_a")
        gw_a.connect(_setting(tmp_path, capital=1_000_000.0))
        gw_b = QmtSimGateway(ee, "QMT_SIM_b")
        gw_b.connect(_setting(tmp_path, capital=1_000_000.0))

        # 给 gw_a 喂 synthetic tick 才能撮合 (set_synthetic_tick 签名 = vt_symbol, last_price)
        gw_a.md.set_synthetic_tick("600000.SSE", 10.0)
        # 在 gw_a 上发单
        oid_a = _buy(gw_a, "600000.SSE", 100, 10.0, ref="strat_a:1")
        assert oid_a, "send_order on gw_a should return vt_orderid"

        # 检查 gw_a 的 orders 表有 1 行 reference=strat_a:1, gw_b 没有
        import sqlite3
        con_a = sqlite3.connect(str(tmp_path / "sim_QMT_SIM_a.db"))
        try:
            n_a = con_a.execute("SELECT COUNT(*) FROM sim_orders WHERE reference=?",
                                ("strat_a:1",)).fetchone()[0]
        finally:
            con_a.close()
        assert n_a == 1, f"gw_a orders should have strat_a:1 row, got {n_a}"

        con_b = sqlite3.connect(str(tmp_path / "sim_QMT_SIM_b.db"))
        try:
            n_b = con_b.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0]
        finally:
            con_b.close()
        assert n_b == 0, f"gw_b should have no orders, got {n_b}"
    finally:
        ee.stop()


def test_naming_validator_dual_sim(tmp_path: Path) -> None:
    """R4: vnpy_common.naming.validate_gateway_name 双 sim 名各自合规."""
    from vnpy_common.naming import validate_gateway_name, classify_gateway

    validate_gateway_name("QMT_SIM_a", expected_class="sim")
    validate_gateway_name("QMT_SIM_b", expected_class="sim")
    assert classify_gateway("QMT_SIM_a") == "sim"
    assert classify_gateway("QMT_SIM_b") == "sim"

    # sim 不能用 live 期望 (反例)
    with pytest.raises(ValueError, match="sim"):
        validate_gateway_name("QMT_SIM_a", expected_class="live")


def test_event_engine_isolation_no_cross_event(tmp_path: Path) -> None:
    """R1: 同 EventEngine 挂两 gateway, on_order/on_trade 不串味.

    两 gateway 各自的 OrderData.gateway_name 字段必须正确, vnpy 主流程才能按
    vt_orderid 路由回正确策略.
    """
    ee = EventEngine()
    ee.start()
    received: list = []
    try:
        from vnpy.trader.event import EVENT_ORDER

        def capture(event):
            order = event.data
            received.append((order.vt_orderid, order.gateway_name))

        ee.register(EVENT_ORDER, capture)

        gw_a = QmtSimGateway(ee, "QMT_SIM_a")
        gw_a.connect(_setting(tmp_path))
        gw_b = QmtSimGateway(ee, "QMT_SIM_b")
        gw_b.connect(_setting(tmp_path))

        # 在 gw_a 发单 → on_order 应仅看到 gateway_name=QMT_SIM_a
        gw_a.md.set_synthetic_tick("600001.SSE", 10.0)
        _buy(gw_a, "600001.SSE", 100, 10.0)

        # 让 EventEngine 处理事件
        import time
        time.sleep(0.3)

        gw_a_events = [e for e in received if e[1] == "QMT_SIM_a"]
        gw_b_events = [e for e in received if e[1] == "QMT_SIM_b"]
        assert len(gw_a_events) >= 1, f"应至少 1 个 QMT_SIM_a 事件, got {received}"
        assert len(gw_b_events) == 0, f"QMT_SIM_b 不应有事件, got {gw_b_events}"
    finally:
        ee.stop()


def test_dual_sim_concurrent_settle_isolation(tmp_path: Path) -> None:
    """gateway A settle_end_of_day 不影响 gateway B 的 last_settle_date."""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_a")
        gw_a.connect(_setting(tmp_path))
        gw_b = QmtSimGateway(ee, "QMT_SIM_b")
        gw_b.connect(_setting(tmp_path))

        gw_a.td.counter.settle_end_of_day(date(2026, 4, 30))
        # gw_a last_settle_date 推进, gw_b 不变 (None)
        assert gw_a.td.counter.last_settle_date == date(2026, 4, 30)
        assert gw_b.td.counter.last_settle_date is None
    finally:
        ee.stop()
