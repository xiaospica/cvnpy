"""多 QmtSimGateway 实例并存的隔离性测试（方案 Y：单进程多 gateway 多账户）。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from vnpy_qmt_sim import QmtSimGateway
from vnpy_qmt_sim.persistence import AccountAlreadyLockedError, QmtSimPersistence


def _gateway_setting(persist_root: Path, capital: float, account: str | None = None) -> dict:
    setting = {
        "模拟资金": capital,
        "部分成交率": 0.0,
        "拒单率": 0.0,
        "订单超时秒数": 30,
        "成交延迟毫秒": 0,
        "报单上报延迟毫秒": 0,
        "卖出持仓不足拒单": "是",
        "行情源": "",  # 显式禁用 bar_source，测试不依赖外部 parquet
        "启用持久化": "是",
        "持久化目录": str(persist_root),
    }
    if account is not None:
        setting["账户"] = account
    return setting


def _buy(gw: QmtSimGateway, vt: str, vol: int, price: float) -> str:
    sym, ex = vt.split(".")
    req = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=price, volume=vol, reference="",
    )
    return gw.send_order(req)


def test_default_account_id_falls_back_to_gateway_name(tmp_path: Path) -> None:
    """未显式配置账户名时，account_id 应等于 gateway_name（避免多 gateway 冲突）。"""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_A")
        gw_a.connect(_gateway_setting(tmp_path, capital=1_000_000.0, account=None))
        gw_b = QmtSimGateway(ee, "QMT_SIM_B")
        gw_b.connect(_gateway_setting(tmp_path, capital=2_000_000.0, account=None))

        assert (tmp_path / "sim_QMT_SIM_A.db").exists()
        assert (tmp_path / "sim_QMT_SIM_B.db").exists()

        assert gw_a.td.counter.accountid == "QMT_SIM_A"
        assert gw_b.td.counter.accountid == "QMT_SIM_B"
    finally:
        ee.stop()


def test_two_gateways_have_independent_capital_and_positions(tmp_path: Path) -> None:
    """两个 gateway 各自下单 → 资金/持仓互不影响。"""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_A")
        gw_a.connect(_gateway_setting(tmp_path, capital=1_000_000.0))
        gw_b = QmtSimGateway(ee, "QMT_SIM_B")
        gw_b.connect(_gateway_setting(tmp_path, capital=2_000_000.0))

        _buy(gw_a, "000001.SZSE", 200, 11.0)
        _buy(gw_b, "600000.SSE", 500, 8.0)

        a_pos = gw_a.td.counter.positions
        b_pos = gw_b.td.counter.positions
        assert f"000001.SZSE.{Direction.LONG.value}" in a_pos
        assert f"000001.SZSE.{Direction.LONG.value}" not in b_pos
        assert f"600000.SSE.{Direction.LONG.value}" in b_pos
        assert f"600000.SSE.{Direction.LONG.value}" not in a_pos

        # 资金各自扣减
        assert gw_a.td.counter.capital < 1_000_000.0
        assert gw_b.td.counter.capital < 2_000_000.0
        # gw_a 起始资金更小，扣减后绝对资金小于 gw_b
        assert gw_a.td.counter.capital < gw_b.td.counter.capital
    finally:
        ee.stop()


def test_explicit_account_overrides_gateway_name_default(tmp_path: Path) -> None:
    """显式指定 setting['账户'] 时，按用户配置走（保留向后兼容）。"""
    ee = EventEngine()
    ee.start()
    try:
        gw = QmtSimGateway(ee, "QMT_SIM_X")
        gw.connect(_gateway_setting(tmp_path, capital=1_000_000.0, account="explicit_acc"))
        assert (tmp_path / "sim_explicit_acc.db").exists()
        assert not (tmp_path / "sim_QMT_SIM_X.db").exists()
        assert gw.td.counter.accountid == "explicit_acc"
    finally:
        ee.stop()


def test_lockfile_blocks_second_persistence_on_same_account(tmp_path: Path) -> None:
    """同 account_id 的第二个 QmtSimPersistence 实例（模拟第二个进程）应被 lockfile 拒绝。"""
    p1 = QmtSimPersistence(account_id="ACC_LOCK", root=tmp_path)
    try:
        with pytest.raises(AccountAlreadyLockedError, match="已被另一进程占用"):
            QmtSimPersistence(account_id="ACC_LOCK", root=tmp_path)
    finally:
        p1.close()

    # 释放后可重新获取
    p2 = QmtSimPersistence(account_id="ACC_LOCK", root=tmp_path)
    p2.close()


def test_multi_gateway_persistence_files_are_separate(tmp_path: Path) -> None:
    """多 gateway 的 SQLite 文件物理隔离，互不污染。"""
    ee = EventEngine()
    ee.start()
    try:
        gw_a = QmtSimGateway(ee, "QMT_SIM_csi300")
        gw_a.connect(_gateway_setting(tmp_path, capital=1_000_000.0))
        gw_b = QmtSimGateway(ee, "QMT_SIM_zz500")
        gw_b.connect(_gateway_setting(tmp_path, capital=2_000_000.0))

        _buy(gw_a, "000001.SZSE", 200, 11.0)
        _buy(gw_b, "600000.SSE", 500, 8.0)

        # 各自 db 独立查询
        from vnpy_qmt_sim.persistence import QmtSimPersistence  # noqa
        # 不能新开实例（lockfile 阻断），改为直接读两个 counter 的 _persistence 内部 conn
        rows_a = gw_a.td.counter._persistence._conn.execute(
            "SELECT vt_symbol FROM sim_positions WHERE account_id=?",
            (gw_a.td.counter.accountid,),
        ).fetchall()
        rows_b = gw_b.td.counter._persistence._conn.execute(
            "SELECT vt_symbol FROM sim_positions WHERE account_id=?",
            (gw_b.td.counter.accountid,),
        ).fetchall()

        a_symbols = {r[0] for r in rows_a}
        b_symbols = {r[0] for r in rows_b}
        assert "000001.SZSE" in a_symbols and "600000.SSE" not in a_symbols
        assert "600000.SSE" in b_symbols and "000001.SZSE" not in b_symbols
    finally:
        ee.stop()
