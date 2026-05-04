"""vnpy 端本地回放权益历史 SQLite (A1/B2 解耦后替代 mlearnweb.db 直写).

设计:
  - 路径: ``$QS_DATA_ROOT/state/replay_history.db`` (默认
    ``D:/vnpy_data/state/replay_history.db``); env ``REPLAY_HISTORY_DB`` 可覆盖
  - 单表 ``replay_equity_snapshots`` (PRIMARY KEY = strategy_name, ts)
    UPSERT 写入 — 同一 (策略, 日期) 重跑回放只保留最新一行
  - WAL 模式让 vnpy 主进程写 + vnpy_webtrader endpoint 读不互锁
  - schema 字段 = mlearnweb.db.strategy_equity_snapshots 子集 (缺 node_id /
    engine 这种部署元信息, 那些由 mlearnweb 端 sync service 拉时 enrich)

数据流 (A1/B2):
  vnpy template.py ─→ replay_history.write_snapshot() ─→ 本地 SQLite
                                                              ↑
                                                              ├─ vnpy_webtrader
                                                              │   /api/v1/ml/strategies/{name}/replay/equity_snapshots
                                                              ↓
  mlearnweb replay_equity_sync_service (5min 轮询) ─→ UPSERT mlearnweb.db
                                                              ↓
  前端 LiveTradingStrategyDetailPage 权益曲线

详见 docs/deployment_a1_p21_plan.md §一.2.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS replay_equity_snapshots (
    strategy_name      TEXT    NOT NULL,
    ts                 TEXT    NOT NULL,    -- ISO datetime (回放逻辑日 15:00)
    strategy_value     REAL    NOT NULL,
    account_equity     REAL    NOT NULL,
    positions_count    INTEGER NOT NULL DEFAULT 0,
    raw_variables_json TEXT,
    inserted_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_name, ts)
);
CREATE INDEX IF NOT EXISTS idx_inserted_at ON replay_equity_snapshots (inserted_at);
CREATE INDEX IF NOT EXISTS idx_strategy_ts ON replay_equity_snapshots (strategy_name, ts);
"""


_lock = threading.Lock()
_init_done: Dict[str, bool] = {}  # 按 db_path 缓存,避免每次写都跑 DDL


def _resolve_db_path() -> Path:
    """按 env > 默认顺序解析 db 路径. 路径不依赖文件存在,首次写时自动建.

    优先级:
      1. env ``REPLAY_HISTORY_DB`` (绝对路径)
      2. ``$QS_DATA_ROOT/state/replay_history.db``
      3. ``D:/vnpy_data/state/replay_history.db`` (兜底默认)
    """
    explicit = os.environ.get("REPLAY_HISTORY_DB")
    if explicit:
        return Path(explicit)
    qs_root = os.environ.get("QS_DATA_ROOT", r"D:/vnpy_data")
    return Path(qs_root) / "state" / "replay_history.db"


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """打开 SQLite 连接 + 一次性建表 (按 db_path 缓存初始化标记)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    key = str(db_path)
    if not _init_done.get(key):
        with _lock:
            if not _init_done.get(key):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA_DDL)
                conn.commit()
                _init_done[key] = True
    return conn


def write_snapshot(
    *,
    strategy_name: str,
    ts: datetime,
    strategy_value: float,
    account_equity: float,
    positions_count: int = 0,
    raw_variables: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """UPSERT 一行回放权益快照. 永不抛, 失败 log warn 返 False.

    Parameters
    ----------
    strategy_name : 策略实例名
    ts : 回放**逻辑日**对应时刻 (建议当日 15:00 收盘)
    strategy_value : 策略口径权益 (cash + 持仓市值)
    account_equity : 账户口径权益 (同 strategy_value 即可)
    positions_count : 当日持仓数 (审计用)
    raw_variables : 策略 self.get_variables() dump (审计/调试)
    db_path : 显式 db 路径 (测试用); None 走 _resolve_db_path

    Returns
    -------
    True 写入成功 / False 失败 (路径无写权限等)
    """
    path = db_path or _resolve_db_path()
    try:
        conn = _get_conn(path)
        try:
            conn.execute(
                """
                INSERT INTO replay_equity_snapshots
                    (strategy_name, ts, strategy_value, account_equity,
                     positions_count, raw_variables_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, ts) DO UPDATE SET
                    strategy_value = excluded.strategy_value,
                    account_equity = excluded.account_equity,
                    positions_count = excluded.positions_count,
                    raw_variables_json = excluded.raw_variables_json,
                    inserted_at = CURRENT_TIMESTAMP
                """,
                (
                    strategy_name,
                    ts.isoformat() if isinstance(ts, datetime) else str(ts),
                    float(strategy_value),
                    float(account_equity),
                    int(positions_count),
                    json.dumps(raw_variables or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        logger.warning(
            "[replay_history] write_snapshot(%s, %s) 失败: %s",
            strategy_name, ts, exc,
        )
        return False


def list_snapshots(
    strategy_name: str,
    *,
    since_iso: Optional[str] = None,
    limit: int = 10000,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """查询某策略的回放权益快照列表 (vnpy_webtrader endpoint 调用入口).

    Parameters
    ----------
    strategy_name : 策略实例名 — 必填精确匹配
    since_iso : ISO datetime 字符串; 仅返回 inserted_at >= since 的行 (mlearnweb
        sync service 用本地 max(inserted_at) 作 since 增量拉)
    limit : 单次最多返回行数 (默认 10000, 上限由 endpoint 层 Query 控制)
    db_path : 测试用; None 走 _resolve_db_path

    Returns
    -------
    list of dict: 每行
        {strategy_name, ts, strategy_value, account_equity,
         positions_count, raw_variables, inserted_at}

    db 文件不存在 → 返空列表 (回放未发生过, 不报错).
    """
    path = db_path or _resolve_db_path()
    if not path.exists():
        return []

    try:
        conn = _get_conn(path)
        conn.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT strategy_name, ts, strategy_value, account_equity, "
                "       positions_count, raw_variables_json, inserted_at "
                "FROM replay_equity_snapshots WHERE strategy_name = ?"
            )
            args: list = [strategy_name]
            if since_iso:
                # 用 datetime() 函数让 SQLite 自己解析两边, 容忍 ISO 'T' vs 空格
                # 分隔符 / 缺微秒等格式差异 (CURRENT_TIMESTAMP 默认空格,
                # Python datetime.isoformat() 用 'T').
                sql += " AND datetime(inserted_at) >= datetime(?)"
                args.append(since_iso)
            sql += " ORDER BY ts ASC LIMIT ?"
            args.append(int(limit))
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "[replay_history] list_snapshots(%s) 失败: %s", strategy_name, exc,
        )
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            raw_vars = json.loads(r["raw_variables_json"]) if r["raw_variables_json"] else {}
        except json.JSONDecodeError:
            raw_vars = {}
        out.append({
            "strategy_name": r["strategy_name"],
            "ts": r["ts"],
            "strategy_value": float(r["strategy_value"]),
            "account_equity": float(r["account_equity"]),
            "positions_count": int(r["positions_count"]),
            "raw_variables": raw_vars,
            "inserted_at": r["inserted_at"],
        })
    return out


def count_snapshots(
    strategy_name: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> int:
    """返回行数 (审计 / 测试用); strategy_name=None 时返全表行数."""
    path = db_path or _resolve_db_path()
    if not path.exists():
        return 0
    try:
        conn = _get_conn(path)
        try:
            if strategy_name:
                row = conn.execute(
                    "SELECT COUNT(*) FROM replay_equity_snapshots WHERE strategy_name=?",
                    (strategy_name,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM replay_equity_snapshots",
                ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("[replay_history] count_snapshots 失败: %s", exc)
        return 0
