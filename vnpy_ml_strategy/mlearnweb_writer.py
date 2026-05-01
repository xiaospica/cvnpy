"""跨工程：vnpy 主进程在回放期间把每日权益快照直接写到 mlearnweb.db。

为何这么做（而非走 mlearnweb HTTP API）：
  - 回放循环里每天产出 1 行 snapshot，N 天 = N 行；走 HTTP fanout 到
    mlearnweb 写需要往返成本 + 鉴权（mlearnweb 写端口受 ops 口令护守）
  - mlearnweb.db 已 WAL，跨进程并发读写安全
  - mlearnweb snapshot_loop 也会自然继续写实时心跳（按 wall-clock 时间）
    与回放历史快照各自管自己的时段，最终曲线连续

关键约束：
  - 表 schema 由 mlearnweb 端定义（``strategy_equity_snapshots``），如该表
    DDL 漂移本模块需同步；提交字段为已知最稳定的子集
  - mlearnweb.db 路径必须可达 — 通过环境变量 MLEARNWEB_DB 注入；缺失时
    本模块 silently skip（log warn 一次），不阻塞回放主循环
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 与 mlearnweb backend 部署位置同机时的默认路径推断（开发环境约定）
_DEFAULT_MLEARNWEB_DB = Path(r"F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db")

# 进程内单例：避免重复 connect / 重复 warn
_lock = threading.Lock()
_warned_missing = False


def _resolve_db_path() -> Optional[Path]:
    """按优先级解析 mlearnweb.db 路径。"""
    env_path = os.environ.get("MLEARNWEB_DB")
    if env_path:
        return Path(env_path)
    if _DEFAULT_MLEARNWEB_DB.exists():
        return _DEFAULT_MLEARNWEB_DB
    return None


def write_replay_equity_snapshot(
    *,
    node_id: str,
    engine: str,
    strategy_name: str,
    ts: datetime,
    strategy_value: float,
    account_equity: float,
    source_label: str = "replay_settle",
    positions_count: int = 0,
    raw_variables: Optional[Dict[str, Any]] = None,
) -> bool:
    """写一行回放权益快照到 mlearnweb.db ``strategy_equity_snapshots``。

    Parameters
    ----------
    node_id : 与 mlearnweb vnpy_nodes.yaml 一致（如 "local"）
    engine : 一般 "MlStrategy"
    strategy_name : 策略实例名
    ts : 回放**逻辑日**对应时刻（建议 当日 15:00 收盘）
    strategy_value : 策略口径权益（cash + 持仓市值）
    account_equity : 账户口径权益（同 strategy_value 即可）
    source_label : 写入源标签，默认 "replay_settle"（与实时 snapshot_loop 区分）

    Returns
    -------
    True 成功 / False 跳过（路径未配置或错误）。永不抛异常。
    """
    global _warned_missing

    db_path = _resolve_db_path()
    if db_path is None:
        with _lock:
            if not _warned_missing:
                # vnpy stdlib logger 默认无 handler 会被 loguru 兜底丢失，
                # 用 print 保证 user 能在 console 看到一次警告。
                msg = (
                    f"[mlearnweb_writer] MLEARNWEB_DB 未配置且默认路径不存在 {_DEFAULT_MLEARNWEB_DB}, "
                    "回放权益曲线不会写入 mlearnweb，前端只能看实时 snapshot_loop 的数据点"
                )
                logger.warning(msg)
                print(msg)  # ← stderr/stdout 兜底
                _warned_missing = True
        return False

    # 异常**不**在此处吞，由调用方 (template._persist_replay_equity_snapshot)
    # 用 self.write_log 上报到 vnpy loguru — 否则 stdlib logger 无 handler 静默丢失，
    # 前几十天写失败 user 不知道。
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # 幂等：用户重跑回放时，同(策略,日期,source_label)只保留最新一行
        conn.execute(
            """
            DELETE FROM strategy_equity_snapshots
            WHERE node_id=? AND engine=? AND strategy_name=?
              AND source_label=? AND DATE(ts)=DATE(?)
            """,
            (node_id, engine, strategy_name, source_label, ts),
        )
        conn.execute(
            """
            INSERT INTO strategy_equity_snapshots
                (node_id, engine, strategy_name, ts,
                 strategy_value, source_label, account_equity,
                 positions_count, raw_variables_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id, engine, strategy_name, ts,
                float(strategy_value), source_label, float(account_equity),
                int(positions_count),
                json.dumps(raw_variables or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def write_replay_ml_metric_snapshot(
    *,
    node_id: str,
    engine: str,
    strategy_name: str,
    trade_date: datetime,
    metrics: Optional[Dict[str, Any]] = None,
    topk_summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """写一行 ml_metric_snapshots（trade_date=回放逻辑日）+ 一行 ml_prediction_daily。

    回放期间 mlearnweb 的 ml_snapshot_loop 拉的是 wall-clock 当前 metrics，
    所有点都堆在启动时刻。本函数让 vnpy 这边在每个回放日 settle 后**主动按
    逻辑日**写一行，前端历史回溯就能看到完整时间序列。

    metrics 期望含 ic / rank_ic / psi_mean / psi_max / pred_mean / pred_std / ...
    topk_summary 期望含 topk(list) / score_histogram / n_symbols ...
    """
    db_path = _resolve_db_path()
    if db_path is None:
        return False

    metrics = metrics or {}
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # ml_metric_snapshots: UPSERT 同 (node, engine, strategy, trade_date)
        conn.execute(
            """
            DELETE FROM ml_metric_snapshots
            WHERE node_id=? AND engine=? AND strategy_name=? AND DATE(trade_date)=DATE(?)
            """,
            (node_id, engine, strategy_name, trade_date),
        )
        conn.execute(
            """
            INSERT INTO ml_metric_snapshots
                (node_id, engine, strategy_name, trade_date,
                 ic, rank_ic, psi_mean, psi_max, psi_n_over_0_25,
                 psi_by_feature_json, ks_by_feature_json,
                 pred_mean, pred_std, pred_zero_ratio, n_predictions,
                 feat_missing_json, model_run_id, core_version, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id, engine, strategy_name, trade_date,
                metrics.get("ic"), metrics.get("rank_ic"),
                metrics.get("psi_mean"), metrics.get("psi_max"),
                metrics.get("psi_n_over_0_25"),
                json.dumps(metrics.get("psi_by_feature") or {}, ensure_ascii=False),
                json.dumps(metrics.get("ks_by_feature") or {}, ensure_ascii=False),
                metrics.get("pred_mean"), metrics.get("pred_std"),
                metrics.get("pred_zero_ratio"), metrics.get("n_predictions"),
                json.dumps(metrics.get("feat_missing") or {}, ensure_ascii=False),
                metrics.get("model_run_id"), metrics.get("core_version"),
                metrics.get("status"),
            ),
        )

        if topk_summary:
            conn.execute(
                """
                DELETE FROM ml_prediction_daily
                WHERE node_id=? AND engine=? AND strategy_name=? AND DATE(trade_date)=DATE(?)
                """,
                (node_id, engine, strategy_name, trade_date),
            )
            conn.execute(
                """
                INSERT INTO ml_prediction_daily
                    (node_id, engine, strategy_name, trade_date,
                     topk_json, score_histogram_json, n_symbols, coverage_ratio,
                     pred_mean, pred_std, model_run_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id, engine, strategy_name, trade_date,
                    json.dumps(topk_summary.get("topk") or [], ensure_ascii=False),
                    json.dumps(topk_summary.get("score_histogram") or [], ensure_ascii=False),
                    topk_summary.get("n_symbols"),
                    topk_summary.get("coverage_ratio"),
                    topk_summary.get("pred_mean"), topk_summary.get("pred_std"),
                    topk_summary.get("model_run_id"), topk_summary.get("status"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return True
