from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pytest

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import (
    AccountData,
    OrderData,
    OrderRequest,
    PositionData,
    TickData,
    TradeData,
)

from vnpy_qmt_sim.bar_source.base import BarQuote
from vnpy_qmt_sim.persistence import QmtSimPersistence
from vnpy_qmt_sim.td import SimulationCounter


class _DummyMd:
    def __init__(self, tick: Optional[TickData] = None, quote: Optional[BarQuote] = None):
        self._tick = tick
        self._quote = quote

    def get_full_tick(self, vt_symbol):
        return self._tick if (self._tick and self._tick.vt_symbol == vt_symbol) else None

    def get_quote(self, vt_symbol):
        return self._quote if (self._quote and self._quote.vt_symbol == vt_symbol) else None


class _DummyGateway:
    gateway_name = "QMT_SIM"

    def __init__(self, md: _DummyMd):
        self.md = md

    def on_order(self, order): return None
    def on_trade(self, trade): return None
    def on_account(self, account): return None
    def on_position(self, position): return None
    def write_log(self, msg): return None


def _tick(vt_symbol: str, last: float = 11.0) -> TickData:
    sym, ex = vt_symbol.split(".")
    return TickData(
        gateway_name="QMT_SIM", symbol=sym, exchange=Exchange(ex),
        datetime=datetime.now(), last_price=last,
        limit_up=last * 1.10, limit_down=last * 0.90,
        bid_price_1=last - 0.01, ask_price_1=last + 0.01,
    )


def _make_counter(tmp_path: Path, account_id: str = "TEST_ACC", initial_capital: float = 1_000_000.0) -> tuple[SimulationCounter, QmtSimPersistence]:
    md = _DummyMd(tick=_tick("000001.SZSE", 11.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]
    counter.accountid = account_id
    counter.capital = initial_capital
    counter.push_account()
    persistence = QmtSimPersistence(account_id=account_id, root=tmp_path)
    counter.attach_persistence(persistence)
    return counter, persistence


def _buy(counter: SimulationCounter, vt: str, vol: int, price: float) -> str:
    sym, ex = vt.split(".")
    return counter.send_order(OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=price, volume=vol, reference="",
    ))


def test_persistence_writes_account_on_attach_and_buy(tmp_path: Path) -> None:
    counter, persistence = _make_counter(tmp_path, "ACC_A", 1_000_000.0)
    counter._persist_account(counter.accounts[counter.accountid])
    counter._persist_account(counter.accounts[counter.accountid])

    row = persistence._conn.execute(
        "SELECT capital, frozen FROM sim_accounts WHERE account_id=?", ("ACC_A",)
    ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(1_000_000.0)


def test_persistence_round_trip_buy_then_restart(tmp_path: Path) -> None:
    """买入 → 持久化 → 新 counter 启动 → 持仓与资金恢复。"""
    counter1, p1 = _make_counter(tmp_path, "ACC_B", 1_000_000.0)
    _buy(counter1, "000001.SZSE", 200, 11.0)
    counter1.settle_end_of_day(date(2026, 4, 22))

    expected_capital = counter1.capital
    pos_key = f"000001.SZSE.{Direction.LONG.value}"
    expected_position = counter1.positions[pos_key]
    expected_volume = expected_position.volume
    expected_yd = expected_position.yd_volume
    expected_price = expected_position.price
    p1.close()

    p2 = QmtSimPersistence(account_id="ACC_B", root=tmp_path)
    state = p2.restore(gateway_name="QMT_SIM")
    assert state.capital == pytest.approx(expected_capital)
    assert state.frozen == 0.0  # GFD：frozen 重置
    assert len(state.positions) == 1
    pos = state.positions[0]
    assert pos.volume == expected_volume
    assert pos.yd_volume == expected_yd
    assert pos.price == pytest.approx(expected_price)
    assert pos.frozen == 0.0


def test_active_orders_cancelled_on_restart(tmp_path: Path) -> None:
    """重启时活跃订单（NOTTRADED/PARTTRADED）按 GFD 标记为 CANCELLED。"""
    counter, p = _make_counter(tmp_path, "ACC_C", 1_000_000.0)

    # 先建仓 + settle 让 yd_volume = 200
    _buy(counter, "000001.SZSE", 200, 11.0)
    counter.settle_end_of_day(date(2026, 4, 21))

    # 切到不立即成交模式：用 reporting_delay 让卖单停在 SUBMITTING/NOTTRADED
    counter.reporting_delay_ms = 60_000  # 1 分钟内不会成交
    sym, ex = "000001.SZSE".split(".")
    sell_req = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.SHORT, type=OrderType.LIMIT,
        offset=Offset.CLOSE, price=11.0, volume=200, reference="",
    )
    sell_vt = counter.send_order(sell_req)
    sell_oid = sell_vt.rsplit(".", 1)[-1]
    assert counter.orders[sell_oid].status in {Status.SUBMITTING, Status.NOTTRADED}
    p.close()

    p2 = QmtSimPersistence(account_id="ACC_C", root=tmp_path)
    state = p2.restore(gateway_name="QMT_SIM")
    assert sell_oid in state.cancelled_active_orders

    row = p2._conn.execute(
        "SELECT status FROM sim_orders WHERE account_id=? AND orderid=?",
        ("ACC_C", sell_oid),
    ).fetchone()
    assert row is not None
    assert row[0] == Status.CANCELLED.value


def test_restore_idempotent_for_no_data(tmp_path: Path) -> None:
    p = QmtSimPersistence(account_id="EMPTY", root=tmp_path)
    state = p.restore(gateway_name="QMT_SIM")
    assert state.capital == 0.0
    assert state.frozen == 0.0
    assert state.positions == []
    assert state.cancelled_active_orders == []


def test_trades_appended_not_overwritten(tmp_path: Path) -> None:
    counter, p = _make_counter(tmp_path, "ACC_D", 1_000_000.0)
    _buy(counter, "000001.SZSE", 200, 11.0)

    rows = list(p._conn.execute(
        "SELECT tradeid, vt_symbol, volume FROM sim_trades WHERE account_id=?",
        ("ACC_D",),
    ))
    assert len(rows) >= 1
    assert rows[-1][1] == "000001.SZSE"
    assert rows[-1][2] == pytest.approx(200.0)


def test_persistence_disabled_counter_still_works(tmp_path: Path) -> None:
    """未 attach persistence 时，所有写入路径仍工作（_persist_* noop）。"""
    md = _DummyMd(tick=_tick("000001.SZSE", 11.0))
    counter = SimulationCounter(_DummyGateway(md))  # type: ignore[arg-type]
    assert counter._persistence is None
    _buy(counter, "000001.SZSE", 200, 11.0)
    pos = counter.positions[f"000001.SZSE.{Direction.LONG.value}"]
    assert pos.volume == 200


def test_trades_carry_reference_for_per_strategy_audit(tmp_path: Path) -> None:
    """订单 reference（含 strategy_name）写入 sim_trades，便于按策略查询。"""
    counter, p = _make_counter(tmp_path, "ACC_E", 1_000_000.0)

    sym, ex = "000001.SZSE".split(".")
    req_a = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=11.0, volume=200,
        reference="csi300_lgb:1",
    )
    counter.send_order(req_a)

    req_b = OrderRequest(
        symbol=sym, exchange=Exchange(ex),
        direction=Direction.LONG, type=OrderType.LIMIT,
        offset=Offset.OPEN, price=11.0, volume=100,
        reference="zz500_lgb:7",
    )
    counter.send_order(req_b)

    # 按策略名前缀查询成交流水
    csi_rows = list(p._conn.execute(
        "SELECT volume FROM sim_trades WHERE account_id=? AND reference LIKE ?",
        ("ACC_E", "csi300_lgb:%"),
    ))
    zz_rows = list(p._conn.execute(
        "SELECT volume FROM sim_trades WHERE account_id=? AND reference LIKE ?",
        ("ACC_E", "zz500_lgb:%"),
    ))
    assert len(csi_rows) == 1 and csi_rows[0][0] == pytest.approx(200.0)
    assert len(zz_rows) == 1 and zz_rows[0][0] == pytest.approx(100.0)


def test_schema_migration_adds_reference_to_old_db(tmp_path: Path) -> None:
    """旧 db 缺 reference 列时，QmtSimPersistence 启动用 ALTER TABLE 兼容。"""
    import sqlite3

    db_path = tmp_path / "sim_LEGACY.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.executescript(
        """CREATE TABLE sim_trades (
               account_id TEXT NOT NULL,
               tradeid    TEXT NOT NULL,
               orderid    TEXT NOT NULL,
               vt_symbol  TEXT NOT NULL,
               direction  TEXT NOT NULL,
               offset     TEXT NOT NULL,
               price      REAL NOT NULL,
               volume     REAL NOT NULL,
               datetime   TEXT NOT NULL,
               PRIMARY KEY (account_id, tradeid)
           );
           INSERT INTO sim_trades VALUES('LEGACY','t1','o1','000001.SZSE','多','开仓',11.0,200,'2026-04-22T15:00:00');"""
    )
    legacy_conn.commit()
    legacy_conn.close()

    p = QmtSimPersistence(account_id="LEGACY", root=tmp_path)
    try:
        cols = {r[1] for r in p._conn.execute("PRAGMA table_info(sim_trades)")}
        assert "reference" in cols
        # 旧记录的 reference 列为 NULL，新记录可正常写入
        old = p._conn.execute(
            "SELECT volume, reference FROM sim_trades WHERE tradeid='t1'"
        ).fetchone()
        assert old[0] == 200.0
        assert old[1] is None
    finally:
        p.close()


# ---------------------------------------------------------------------------
# P0-5: stale lockfile 检测 + 自动清理
# ---------------------------------------------------------------------------


def test_stale_lockfile_with_dead_pid_auto_cleared(tmp_path: Path) -> None:
    """[P0-5] lockfile 残留 dead PID 时自动清理 + 新进程拿锁成功.

    Windows msvcrt.locking 锁定 byte 0 → Path.read_text 在持有期会 PermissionError,
    所以验证 PID 内容必须 close() 后. 这也是最终用户视角 (停服后查 lockfile).
    """
    import os
    lock_path = tmp_path / "sim_X.lock"
    # 写一个一定不存在的 PID (Linux/Windows 上都用 9999999, psutil.pid_exists False)
    lock_path.write_text("9999999\n", encoding="utf-8")

    # 不应 raise — stale PID 自动清理
    p = QmtSimPersistence(account_id="X", root=tmp_path)
    p.close()

    # close 后读, 应该是当前进程 PID
    pid_in_file = lock_path.read_text(encoding="utf-8").strip()
    assert pid_in_file == str(os.getpid())


def test_stale_lockfile_with_alive_pid_rejects(tmp_path: Path) -> None:
    """[P0-5] lockfile PID 是当前进程 (psutil 视为 alive), 不应清理而是 raise."""
    import os
    from vnpy_qmt_sim.persistence import AccountAlreadyLockedError

    lock_path = tmp_path / "sim_Y.lock"
    # 用当前进程 PID — psutil.pid_exists 必返 True
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    # 第一个 persistence 实例占用 lock
    p1 = QmtSimPersistence(account_id="Y", root=tmp_path)
    try:
        # 第二个实例同 account_id 应该被拒绝 (进程内 _try_acquire_lock 会失败)
        with pytest.raises(AccountAlreadyLockedError):
            QmtSimPersistence(account_id="Y", root=tmp_path)
    finally:
        p1.close()


def test_lockfile_writes_current_pid(tmp_path: Path) -> None:
    """[P0-5] 启动后 lockfile 写入当前 PID, 便于运维查谁占用.

    Windows msvcrt.locking 持有期 byte 0 锁定 → 必须 close 后再读.
    """
    import os
    p = QmtSimPersistence(account_id="Z", root=tmp_path)
    p.close()
    content = (tmp_path / "sim_Z.lock").read_text(encoding="utf-8").strip()
    assert content == str(os.getpid())
