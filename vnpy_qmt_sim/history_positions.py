"""跨机部署支持 — 在 vnpy_qmt_sim 节点端实现"重建任意 yyyymmdd EOD 持仓".

由 vnpy_webtrader endpoint /api/v1/position/history/{strategy_name}/{yyyymmdd}
调用; mlearnweb 通过 fanout 拉取，避免 mlearnweb 直读节点本地 sim db 文件
（跨机部署下不可达）。

逻辑与 mlearnweb backend/services/vnpy/historical_positions_service.py 同源:
  1. 按 datetime 升序遍历 sim_trades 累计 (volume, cost) 到 target_date EOD
  2. 每个交易日结束 cost *= (1 + pct_chg/100) 模拟 settle mark-to-market
  3. 输出 EOD (volume>0) 持仓 + market_value (vol×cost) + weight (持仓内部 sum=1)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# 同机部署默认路径 (与 vnpy_qmt_sim/persistence.py 一致)
_DEFAULT_TRADING_STATE = Path(__file__).resolve().parent / ".trading_state"
_DEFAULT_DAILY_MERGED = Path(r"D:/vnpy_data/stock_data/daily_merged_all_new.parquet")


def _vt_to_ts(vt: str) -> str:
    if vt.endswith(".SZSE"): return vt[:-5] + ".SZ"
    if vt.endswith(".SSE"):  return vt[:-4] + ".SH"
    if vt.endswith(".BSE"):  return vt[:-4] + ".BJ"
    return vt


def _resolve_sim_db_path(gateway_name: str) -> Optional[Path]:
    """sim_<account_id>.db, account_id 默认 == gateway_name."""
    root = Path(os.environ.get("VNPY_QMT_SIM_TRADING_STATE", _DEFAULT_TRADING_STATE))
    if not root.exists():
        return None
    if gateway_name:
        p = root / f"sim_{gateway_name}.db"
        if p.exists():
            return p
    # fallback: 任何 sim_*.db
    for p in root.glob("sim_*.db"):
        return p
    return None


def build_positions_on_date(
    strategy_name: str,
    target_date_str: str,
    gateway_name: str = "",
) -> List[Dict[str, Any]]:
    """重建 EOD 持仓快照. 失败返空 list (vnpy webtrader endpoint 不抛异常)."""
    try:
        target_d = datetime.strptime(target_date_str, "%Y%m%d").date()
    except ValueError:
        logger.warning(f"[history_positions] invalid date: {target_date_str}")
        return []

    db_path = _resolve_sim_db_path(gateway_name)
    if db_path is None:
        logger.warning(f"[history_positions] sim db 不可达 (gateway={gateway_name})")
        return []

    merged_path = Path(os.environ.get("DAILY_MERGED_ALL_PATH", _DEFAULT_DAILY_MERGED))
    if not merged_path.exists():
        logger.warning(f"[history_positions] daily_merged 不存在: {merged_path}")
        return []
    try:
        merged = pd.read_parquet(merged_path)
        merged["trade_date"] = pd.to_datetime(merged["trade_date"])
    except Exception as e:
        logger.warning(f"[history_positions] 读 merged 失败: {e}")
        return []
    pct_lookup: Dict[Tuple[str, pd.Timestamp], float] = (
        merged.set_index(["ts_code", "trade_date"])["pct_chg"].to_dict()
    )

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT vt_symbol, direction, volume, price, datetime, reference "
            "FROM sim_trades ORDER BY datetime ASC"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"[history_positions] 读 sim db 失败: {e}")
        return []

    by_day: Dict[date, List[Tuple[str, str, float, float]]] = defaultdict(list)
    for vt, direction, volume, price, dt_str, reference in rows:
        if reference and strategy_name and not str(reference).startswith(f"{strategy_name}:"):
            continue
        try:
            dt = datetime.fromisoformat(dt_str) if isinstance(dt_str, str) else dt_str
        except Exception:
            continue
        d = dt.date() if hasattr(dt, "date") else dt
        if d > target_d:
            break
        by_day[d].append((vt, direction, float(volume), float(price)))

    pos: Dict[str, Dict[str, float]] = {}
    for d in sorted(by_day):
        for vt, direction, vol, price in by_day[d]:
            if direction in ("LONG", "多", "Direction.LONG"):
                old = pos.get(vt, {"vol": 0.0, "cost": 0.0})
                new_v = old["vol"] + vol
                pos[vt] = {
                    "vol": new_v,
                    "cost": (old["vol"] * old["cost"] + vol * price) / new_v if new_v > 0 else 0,
                }
            else:
                old_v = pos.get(vt, {"vol": 0.0})["vol"]
                if old_v > 0:
                    pos[vt]["vol"] = old_v - vol
                    if pos[vt]["vol"] <= 0:
                        del pos[vt]
        for vt in list(pos.keys()):
            ts = _vt_to_ts(vt)
            pct = pct_lookup.get((ts, pd.Timestamp(d)))
            if pct is not None and pd.notna(pct):
                pos[vt]["cost"] *= (1.0 + float(pct) / 100.0)

    holdings = [(vt, p) for vt, p in pos.items() if p["vol"] > 0]
    total_mv = sum(p["vol"] * p["cost"] for _, p in holdings)
    out: List[Dict[str, Any]] = []
    for vt, p in holdings:
        mv = p["vol"] * p["cost"]
        out.append({
            "vt_symbol": vt,
            "volume": p["vol"],
            "cost_price": round(p["cost"], 4),
            "market_value": round(mv, 2),
            "weight": (mv / total_mv) if total_mv > 0 else 0.0,
        })
    out.sort(key=lambda r: r["market_value"], reverse=True)
    return out
