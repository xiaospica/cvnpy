"""SQLite 跨日持久化层。账户 / 持仓 / 订单 / 成交。

设计要点：
- 单文件 SQLite（每账户独立文件 sim_{account_id}.db），WAL 模式，避免与 mlearnweb.db 互锁
- 即时写：每次状态变化在内存 + DB 双写，简化崩溃恢复
- 跨日活跃订单按 A 股 GFD 规则（当日有效）重启时全部 CANCELLED，frozen 资金/持仓清零
- 历史成交流水只追加、不删除，供审计 / 回看
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import AccountData, OrderData, PositionData, TradeData

logger = logging.getLogger(__name__)


class AccountAlreadyLockedError(RuntimeError):
    """同 account_id 已被另一进程占用。第二进程启动时抛出，避免内存状态分离导致数据竞争。"""


def _try_acquire_lock(fd: int) -> bool:
    """获取独占非阻塞文件锁。成功返回 True，被占用返回 False。"""
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False


def _release_lock(fd: int) -> None:
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_accounts (
    account_id TEXT PRIMARY KEY,
    capital    REAL NOT NULL,
    frozen     REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_positions (
    account_id TEXT NOT NULL,
    vt_symbol  TEXT NOT NULL,
    direction  TEXT NOT NULL,
    volume     REAL NOT NULL,
    yd_volume  REAL NOT NULL,
    frozen     REAL NOT NULL,
    price      REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, vt_symbol, direction)
);

CREATE TABLE IF NOT EXISTS sim_orders (
    account_id TEXT NOT NULL,
    orderid    TEXT NOT NULL,
    vt_symbol  TEXT NOT NULL,
    direction  TEXT NOT NULL,
    offset     TEXT NOT NULL,
    order_type TEXT NOT NULL,
    price      REAL NOT NULL,
    volume     REAL NOT NULL,
    traded     REAL NOT NULL,
    status     TEXT NOT NULL,
    status_msg TEXT,
    reference  TEXT,
    datetime   TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, orderid)
);
CREATE INDEX IF NOT EXISTS ix_sim_orders_status ON sim_orders(account_id, status);

CREATE TABLE IF NOT EXISTS sim_trades (
    account_id TEXT NOT NULL,
    tradeid    TEXT NOT NULL,
    orderid    TEXT NOT NULL,
    vt_symbol  TEXT NOT NULL,
    direction  TEXT NOT NULL,
    offset     TEXT NOT NULL,
    price      REAL NOT NULL,
    volume     REAL NOT NULL,
    datetime   TEXT NOT NULL,
    reference  TEXT,
    PRIMARY KEY (account_id, tradeid)
);
CREATE INDEX IF NOT EXISTS ix_sim_trades_datetime ON sim_trades(datetime);
-- ix_sim_trades_reference 由 _migrate() 创建（旧 db 该列后加，CREATE INDEX 必须在 ALTER 之后）
"""

_ACTIVE_ORDER_STATUSES = {Status.SUBMITTING, Status.NOTTRADED, Status.PARTTRADED}


@dataclass
class RestoredState:
    capital: float
    frozen: float
    positions: list[PositionData]
    cancelled_active_orders: list[str]


class QmtSimPersistence:
    def __init__(self, account_id: str, root: str | Path) -> None:
        self.account_id = account_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / f"sim_{account_id}.db"
        self.lock_path = self.root / f"sim_{account_id}.lock"
        self._lock = threading.RLock()

        self._lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT)
        if not _try_acquire_lock(self._lock_fd):
            os.close(self._lock_fd)
            self._lock_fd = -1
            raise AccountAlreadyLockedError(
                f"账户 {account_id!r} 的持久化文件已被另一进程占用 ({self.lock_path})。"
                f"同机多进程使用同一 account_id 会导致数据竞争；请用不同的 account_id "
                f"（默认 = gateway_name），或确认前一进程已退出后删除 lock 文件。"
            )
        try:
            os.write(self._lock_fd, f"{os.getpid()}\n".encode("utf-8"))
        except OSError:
            pass

        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
        except Exception:
            self._release_process_lock()
            raise

    def _migrate(self) -> None:
        """schema 演进：旧 db 缺少新增列时按需 ALTER TABLE。幂等。"""
        existing_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sim_trades)")}
        if "reference" not in existing_cols:
            self._conn.execute("ALTER TABLE sim_trades ADD COLUMN reference TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sim_trades_reference "
            "ON sim_trades(account_id, reference)"
        )

    def _release_process_lock(self) -> None:
        if self._lock_fd >= 0:
            _release_lock(self._lock_fd)
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = -1

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
            self._release_process_lock()

    # ---- writes ----

    def upsert_account(self, account: AccountData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sim_accounts(account_id, capital, frozen, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(account_id) DO UPDATE SET
                       capital=excluded.capital,
                       frozen=excluded.frozen,
                       updated_at=excluded.updated_at""",
                (self.account_id, float(account.balance), float(account.frozen), _now()),
            )

    def upsert_position(self, pos: PositionData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sim_positions(account_id, vt_symbol, direction, volume, yd_volume, frozen, price, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, vt_symbol, direction) DO UPDATE SET
                       volume=excluded.volume,
                       yd_volume=excluded.yd_volume,
                       frozen=excluded.frozen,
                       price=excluded.price,
                       updated_at=excluded.updated_at""",
                (
                    self.account_id, pos.vt_symbol, pos.direction.value,
                    float(pos.volume), float(pos.yd_volume), float(pos.frozen),
                    float(pos.price), _now(),
                ),
            )

    def upsert_order(self, order: OrderData) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sim_orders(account_id, orderid, vt_symbol, direction, offset, order_type,
                                          price, volume, traded, status, status_msg, reference, datetime, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_id, orderid) DO UPDATE SET
                       traded=excluded.traded,
                       status=excluded.status,
                       status_msg=excluded.status_msg,
                       updated_at=excluded.updated_at""",
                (
                    self.account_id, order.orderid, order.vt_symbol,
                    order.direction.value, order.offset.value, order.type.value,
                    float(order.price), float(order.volume), float(order.traded),
                    order.status.value, getattr(order, "status_msg", "") or "",
                    getattr(order, "reference", "") or "",
                    _fmt_dt(order.datetime), _now(),
                ),
            )

    def insert_trade(self, trade: TradeData, reference: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sim_trades(account_id, tradeid, orderid, vt_symbol, direction,
                                                    offset, price, volume, datetime, reference)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.account_id, trade.tradeid, trade.orderid, trade.vt_symbol,
                    trade.direction.value, trade.offset.value,
                    float(trade.price), float(trade.volume), _fmt_dt(trade.datetime),
                    reference or "",
                ),
            )

    # ---- restore ----

    def restore(self, gateway_name: str) -> RestoredState:
        """启动时恢复账户与持仓，按 GFD 规则把活跃订单标记为 CANCELLED。

        持仓的 frozen 字段重置为 0（活跃卖单已 cancel，没有理由保留 position frozen）。
        账户的 frozen 字段同理重置为 0。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT capital, frozen FROM sim_accounts WHERE account_id=?",
                (self.account_id,),
            ).fetchone()
            if row is None:
                return RestoredState(capital=0.0, frozen=0.0, positions=[], cancelled_active_orders=[])

            capital = float(row[0])
            # frozen 重置为 0：活跃订单都将被 cancel
            frozen = 0.0

            positions: list[PositionData] = []
            for r in self._conn.execute(
                """SELECT vt_symbol, direction, volume, yd_volume, price
                   FROM sim_positions WHERE account_id=? AND volume > 0""",
                (self.account_id,),
            ):
                vt_symbol, direction, volume, yd_volume, price = r
                symbol, exchange_str = vt_symbol.rsplit(".", 1)
                from vnpy.trader.constant import Exchange
                pos = PositionData(
                    symbol=symbol,
                    exchange=Exchange(exchange_str),
                    direction=Direction(direction),
                    volume=float(volume),
                    yd_volume=float(yd_volume),
                    frozen=0.0,
                    price=float(price),
                    gateway_name=gateway_name,
                )
                positions.append(pos)

            cancelled_orderids = []
            cur = self._conn.execute(
                "SELECT orderid, status FROM sim_orders WHERE account_id=? AND status IN (?,?,?)",
                (self.account_id, Status.SUBMITTING.value, Status.NOTTRADED.value, Status.PARTTRADED.value),
            )
            for orderid, _status in cur.fetchall():
                cancelled_orderids.append(orderid)

            if cancelled_orderids:
                placeholders = ",".join("?" for _ in cancelled_orderids)
                self._conn.execute(
                    f"""UPDATE sim_orders SET status=?, status_msg=?, updated_at=?
                        WHERE account_id=? AND orderid IN ({placeholders})""",
                    (Status.CANCELLED.value, "重启自动撤单(GFD)", _now(),
                     self.account_id, *cancelled_orderids),
                )

            # 持仓 frozen 也清零（与活跃卖单 cancel 配套）
            self._conn.execute(
                "UPDATE sim_positions SET frozen=0, updated_at=? WHERE account_id=?",
                (_now(), self.account_id),
            )
            self._conn.execute(
                "UPDATE sim_accounts SET frozen=0, updated_at=? WHERE account_id=?",
                (_now(), self.account_id),
            )

            return RestoredState(
                capital=capital,
                frozen=frozen,
                positions=positions,
                cancelled_active_orders=cancelled_orderids,
            )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds")
