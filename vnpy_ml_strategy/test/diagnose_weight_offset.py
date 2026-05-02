"""严格验证 weight 残余 2.65% 偏差归因 — 不接受猜测.

假设 H1: 偏差源 = 整百取整 + raw_open vs hfq_close 撮合价分母不同
  → vnpy_amount/qlib_amount 比例 ≈ qlib_hfq_close / vnpy_raw_open (买入时), 稳定不漂移
  → vnpy_mv/qlib_mv 在持有期单只股大致恒定

假设 H2: 偏差源 = settle 累乘的 pct_chg 与 hfq 累乘有浮点累积差异
  → vnpy_mv/qlib_mv 在持有期单只股**逐日漂移** (越持越偏)

实证: 取一只两边持有连续多日的股, 逐日 dump amount + price + mv, 看比例演化.
"""
from __future__ import annotations

import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]  # vnpy_strategy_dev
sys.path.insert(0, str(_ROOT / "vendor" / "qlib_strategy_core"))  # qlib (unpickle Position)

QLIB_BT = Path(r"C:/Users/richard/AppData/Local/Temp/qlib_d_backtest")
VNPY_DB = Path(r"F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state/sim_QMT_SIM_csi300.db")
DAILY_MERGED = Path(r"D:/vnpy_data/stock_data/daily_merged_all_new.parquet")


def vt_to_ts(vt: str) -> str:
    if vt.endswith(".SZSE"): return vt[:-5] + ".SZ"
    if vt.endswith(".SSE"):  return vt[:-4] + ".SH"
    return vt


def reconstruct_vnpy_daily(vnpy_db: Path, daily_merged: Path):
    """重建 vnpy 每日 EOD: vt -> (volume, cost_after_settle), 含 settle pct_chg 累乘."""
    merged = pd.read_parquet(daily_merged, columns=["ts_code", "trade_date", "pct_chg", "open", "close"])
    merged["trade_date"] = pd.to_datetime(merged["trade_date"])
    pct_lookup = merged.set_index(["ts_code", "trade_date"])["pct_chg"].to_dict()
    open_lookup = merged.set_index(["ts_code", "trade_date"])["open"].to_dict()
    close_lookup = merged.set_index(["ts_code", "trade_date"])["close"].to_dict()

    conn = sqlite3.connect(str(vnpy_db))
    cur = conn.cursor()
    cur.execute("SELECT vt_symbol, direction, volume, price, datetime FROM sim_trades ORDER BY datetime ASC")
    rows = cur.fetchall()
    conn.close()

    by_day: Dict[pd.Timestamp, List] = defaultdict(list)
    for vt, direction, vol, price, dt_str in rows:
        d = pd.Timestamp(datetime.fromisoformat(dt_str).date())
        by_day[d].append((vt, direction, float(vol), float(price)))

    pos: Dict[str, Dict[str, float]] = {}  # ts -> {vol, cost, today_buy_vol, today_buy_cost}
    daily: Dict[pd.Timestamp, Dict[str, Tuple[float, float]]] = {}
    for d in sorted(by_day):
        # reset today_buy tracking
        for ts in pos:
            pos[ts]["today_buy_vol"] = 0
            pos[ts]["today_buy_cost"] = 0
        for vt, direction, vol, price in by_day[d]:
            ts = vt_to_ts(vt)
            if direction in ("LONG", "多", "Direction.LONG"):
                if ts not in pos:
                    pos[ts] = {"vol": 0, "cost": 0, "today_buy_vol": 0, "today_buy_cost": 0}
                pos[ts]["vol"] += vol
                pos[ts]["today_buy_vol"] += vol
                pos[ts]["today_buy_cost"] += vol * price
                # update overall avg cost (this is the trade.price, will be settle-adjusted EOD)
                pos[ts]["cost"] = price  # vnpy 用最新成交价覆盖, 不是加权平均
            else:
                if ts in pos:
                    pos[ts]["vol"] -= vol
                    if pos[ts]["vol"] <= 0:
                        del pos[ts]
        # settle: 区分新买入 vs 老持仓 (与 td.py:settle_end_of_day 同步)
        for ts in list(pos.keys()):
            p = pos[ts]
            today_vol = p["today_buy_vol"]
            yd_vol = p["vol"] - today_vol
            pct = pct_lookup.get((ts, d))
            opn = open_lookup.get((ts, d))
            cls = close_lookup.get((ts, d))
            if pct is None or opn is None or cls is None or opn <= 0:
                continue
            pct_f = float(pct) / 100.0
            if today_vol > 0 and yd_vol > 0:
                # 混合 - 与 td.py:680 同源
                old_value = (yd_vol * p["cost"]) * (1.0 + pct_f)
                new_value = p["today_buy_cost"] * float(cls) / float(opn)
                p["cost"] = (old_value + new_value) / p["vol"]
            elif today_vol > 0:
                p["cost"] = p["cost"] * float(cls) / float(opn)
            else:
                p["cost"] = p["cost"] * (1.0 + pct_f)
        # snapshot
        daily[d] = {ts: (p["vol"], p["cost"]) for ts, p in pos.items() if p["vol"] > 0}
    return daily


def main():
    # qlib
    qpos = pickle.load(open(QLIB_BT / "positions_normal_1day.pkl", "rb"))
    qlib_daily = {}
    for d, pos_obj in qpos.items():
        if not isinstance(d, pd.Timestamp):
            continue
        details = {}
        for ts, info in pos_obj.position.items():
            if ts in ("cash", "now_account_value"):
                continue
            if isinstance(info, dict):
                details[ts] = (float(info.get("amount", 0)), float(info.get("price", 0)))
        qlib_daily[d.normalize()] = details

    vnpy_daily = reconstruct_vnpy_daily(VNPY_DB, DAILY_MERGED)

    # 找一只两边都持有 连续多日 的股 — 002493.SZ 是首日建仓股
    target = "002493.SZ"
    print(f"=== 单股 {target} 跨日 vnpy vs qlib market_value 比例演化 ===")
    print(f"{'date':12} | {'qlib amount':>12} {'qlib hfq_close':>15} {'qlib mv':>10} | {'vnpy vol':>10} {'vnpy cost':>10} {'vnpy mv':>10} | {'mv ratio (v/q)':>15}")
    print("-" * 130)
    overlap = sorted(set(qlib_daily) & set(vnpy_daily))
    ratios = []
    for d in overlap:
        if target not in qlib_daily[d] or target not in vnpy_daily[d]:
            continue
        q_amt, q_pri = qlib_daily[d][target]
        v_vol, v_cost = vnpy_daily[d][target]
        q_mv = q_amt * q_pri
        v_mv = v_vol * v_cost
        ratio = v_mv / q_mv if q_mv > 0 else 0
        ratios.append(ratio)
        print(f"{str(d.date()):12} | {q_amt:>12.4f} {q_pri:>15.4f} {q_mv:>10.0f} | {v_vol:>10.0f} {v_cost:>10.4f} {v_mv:>10.0f} | {ratio:>15.6f}")

    if ratios:
        import statistics
        print(f"\n比例统计: min={min(ratios):.6f}, max={max(ratios):.6f}, mean={statistics.mean(ratios):.6f}, std={statistics.stdev(ratios) if len(ratios)>1 else 0:.6f}")
        drift = max(ratios) - min(ratios)
        if drift < 0.005:
            print(f"  → 比例稳定 (drift {drift:.4f}<0.5%), 验证 H1: 整百取整+撮合价分母")
        else:
            print(f"  → 比例漂移 (drift {drift:.4f}≥0.5%), 验证 H2: settle 累乘累积差异")

    # 第二只股交叉验证
    print()
    target2 = "000333.SZ"
    print(f"=== 单股 {target2} 跨日 vnpy vs qlib market_value 比例演化 ===")
    print(f"{'date':12} | {'qlib amount':>12} {'qlib hfq_close':>15} {'qlib mv':>10} | {'vnpy vol':>10} {'vnpy cost':>10} {'vnpy mv':>10} | {'mv ratio (v/q)':>15}")
    print("-" * 130)
    ratios2 = []
    for d in overlap:
        if target2 not in qlib_daily[d] or target2 not in vnpy_daily[d]:
            continue
        q_amt, q_pri = qlib_daily[d][target2]
        v_vol, v_cost = vnpy_daily[d][target2]
        q_mv = q_amt * q_pri
        v_mv = v_vol * v_cost
        ratio = v_mv / q_mv if q_mv > 0 else 0
        ratios2.append(ratio)
        print(f"{str(d.date()):12} | {q_amt:>12.4f} {q_pri:>15.4f} {q_mv:>10.0f} | {v_vol:>10.0f} {v_cost:>10.4f} {v_mv:>10.0f} | {ratio:>15.6f}")
    if ratios2:
        import statistics
        print(f"\n比例统计: min={min(ratios2):.6f}, max={max(ratios2):.6f}, mean={statistics.mean(ratios2):.6f}")


if __name__ == "__main__":
    main()
