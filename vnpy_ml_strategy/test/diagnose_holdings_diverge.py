"""严格诊断 vnpy 回放与 qlib backtest holdings diverge 的根因.

不猜测 — 逐日 dump 两边的 (current_holdings, sell, buy, cash, value, amount,
fee_estimate) 全部状态, 找出 first divergence 的精确字段。

输出:
  - first_diverge_day, 该日两边 sell/buy 集合
  - divergence_root_field: 哪个字段 first 不同 (cash / pred score / amount / fee)
  - 对比表: T-1 EOD 状态 + T 日决策 + T 日 EOD 状态
"""
from __future__ import annotations

import json
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]  # vnpy_strategy_dev
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor" / "qlib_strategy_core"))  # qlib (unpickle Position)

QLIB_BT_DIR = Path(r"C:/Users/richard/AppData/Local/Temp/qlib_d_backtest")
VNPY_SIM_DB = Path(r"F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state/sim_QMT_SIM_csi300.db")
ML_OUTPUT = Path(r"D:/ml_output/csi300_lgb_headless")
DAILY_MERGED = Path(r"D:/vnpy_data/stock_data/daily_merged_all_new.parquet")


def vt_to_ts(vt: str) -> str:
    if vt.endswith(".SZSE"): return vt[:-5] + ".SZ"
    if vt.endswith(".SSE"):  return vt[:-4] + ".SH"
    return vt


def ts_to_vt(ts: str) -> str:
    if ts.endswith(".SZ"): return ts[:-3] + ".SZSE"
    if ts.endswith(".SH"): return ts[:-3] + ".SSE"
    return ts


def load_qlib_state() -> Dict[pd.Timestamp, Dict[str, Any]]:
    """qlib positions: {date -> {holdings: set[ts], details: dict[ts, info], cash, total_value}}."""
    pkl = pickle.load(open(QLIB_BT_DIR / "positions_normal_1day.pkl", "rb"))
    out = {}
    for d, pos_obj in pkl.items():
        if not isinstance(d, pd.Timestamp):
            continue
        details = {}
        cash = pos_obj.position.get("cash")
        total = pos_obj.position.get("now_account_value")
        for ts, info in pos_obj.position.items():
            if ts in ("cash", "now_account_value"):
                continue
            if isinstance(info, dict):
                details[ts] = info
        out[d.normalize()] = {
            "holdings": set(details.keys()),
            "details": details,
            "cash": float(cash) if cash else None,
            "total": float(total) if total else None,
        }
    return out


def load_vnpy_state() -> Dict[pd.Timestamp, Dict[str, Any]]:
    """从 sim_trades 重建每日 (T 日 EOD) 状态: holdings, sell_codes, buy_codes, raw_buy_value."""
    conn = sqlite3.connect(str(VNPY_SIM_DB))
    cur = conn.cursor()
    cur.execute(
        "SELECT vt_symbol, direction, volume, price, datetime, reference "
        "FROM sim_trades ORDER BY datetime ASC"
    )
    rows = cur.fetchall()
    conn.close()

    by_day: Dict[pd.Timestamp, List[Tuple[str, str, float, float]]] = defaultdict(list)
    for vt, direction, volume, price, dt_str, ref in rows:
        dt = datetime.fromisoformat(dt_str)
        d = pd.Timestamp(dt.date())
        by_day[d].append((vt, direction, float(volume), float(price)))

    holdings_set: Set[str] = set()
    pos_amounts: Dict[str, float] = {}
    pos_costs: Dict[str, float] = {}
    cumulative_buy_value = 0.0
    cumulative_sell_value = 0.0
    cumulative_fee = 0.0
    init_cash = 1_000_000.0
    out = {}
    for d in sorted(by_day):
        sells: List[Tuple[str, float, float]] = []
        buys: List[Tuple[str, float, float]] = []
        for vt, direction, volume, price in by_day[d]:
            ts = vt_to_ts(vt)
            if direction in ("LONG", "多", "Direction.LONG"):
                buys.append((ts, volume, price))
                old_v = pos_amounts.get(ts, 0)
                old_c = pos_costs.get(ts, 0)
                new_v = old_v + volume
                pos_amounts[ts] = new_v
                pos_costs[ts] = (old_v * old_c + volume * price) / new_v if new_v > 0 else 0
                holdings_set.add(ts)
                cumulative_buy_value += volume * price
                cumulative_fee += volume * price * 0.0001 + 5  # commission + min_commission
            else:
                sells.append((ts, volume, price))
                old_v = pos_amounts.get(ts, 0)
                if old_v > 0:
                    pos_amounts[ts] = old_v - volume
                    if pos_amounts[ts] <= 0:
                        holdings_set.discard(ts)
                        del pos_amounts[ts]
                        if ts in pos_costs:
                            del pos_costs[ts]
                cumulative_sell_value += volume * price
                cumulative_fee += volume * price * (0.0001 + 0.0005) + 5  # + stamp duty
        # cash 估算 (不严格, 用累计值)
        cash_est = init_cash + cumulative_sell_value - cumulative_buy_value - cumulative_fee
        out[d] = {
            "holdings": set(holdings_set),
            "sells": sells,
            "buys": buys,
            "pos_amounts": dict(pos_amounts),
            "pos_costs": dict(pos_costs),
            "cash_estimate": cash_est,
            "raw_buy_value_today": sum(v * p for _, v, p in buys),
            "raw_sell_value_today": sum(v * p for _, v, p in sells),
        }
    return out


def load_pred_score(day: pd.Timestamp) -> pd.Series:
    """读 vnpy 推理产物 predictions.parquet."""
    p = ML_OUTPUT / day.strftime("%Y%m%d") / "predictions.parquet"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_parquet(p)
    if "datetime" in df.index.names:
        df = df.xs(day, level="datetime", drop_level=True)
    return df["score"] if "score" in df.columns else df.iloc[:, 0]


def main() -> int:
    print("=" * 80)
    print("  Holdings Diverge 严格诊断")
    print("=" * 80)

    qlib_state = load_qlib_state()
    vnpy_state = load_vnpy_state()

    overlap = sorted(set(qlib_state) & set(vnpy_state))
    print(f"\n重叠日期: {len(overlap)} 天 ({overlap[0].date()} ~ {overlap[-1].date()})")

    # 1. 找 first divergence day
    first_diverge: pd.Timestamp = None
    for d in overlap:
        q_h = qlib_state[d]["holdings"]
        v_h = vnpy_state[d]["holdings"]
        if q_h != v_h:
            first_diverge = d
            break

    if first_diverge is None:
        print("\n[OK] 全部日期持仓一致 — 无 diverge")
        return 0

    print(f"\nFirst divergence day: {first_diverge.date()}")
    fd_idx = overlap.index(first_diverge)
    prev_d = overlap[fd_idx - 1] if fd_idx > 0 else None
    print(f"前一天 (T-1): {prev_d.date() if prev_d is not None else 'N/A'}")

    # 2. 对比 T-1 EOD 状态 (应该一致才能保证 T 日决策起点同源)
    if prev_d is not None:
        q_prev = qlib_state[prev_d]
        v_prev = vnpy_state[prev_d]
        print(f"\n=== T-1 ({prev_d.date()}) EOD 状态对比 ===")
        if q_prev["holdings"] == v_prev["holdings"]:
            print(f"  持仓集合 {sorted(q_prev['holdings'])} 一致 [OK]")
        else:
            only_q = q_prev["holdings"] - v_prev["holdings"]
            only_v = v_prev["holdings"] - q_prev["holdings"]
            print(f"  [FAIL] T-1 持仓已不一致 — 真正的 first diverge 在更早")
            print(f"    only_qlib: {only_q}")
            print(f"    only_vnpy: {only_v}")
            return 1
        # cash 对比
        q_cash = q_prev["cash"]
        v_cash = v_prev["cash_estimate"]
        diff = abs(q_cash - v_cash) if q_cash and v_cash else None
        print(f"  qlib cash:  {q_cash:.2f}")
        print(f"  vnpy cash:  {v_cash:.2f} (估算, 含 cumulative fee 0.0001+0.0005+5)")
        print(f"  cash diff:  {diff:.2f} ({diff/q_cash*100:.3f}%)")

    # 3. T 日决策对比
    print(f"\n=== T ({first_diverge.date()}) 决策对比 ===")
    q_d = qlib_state[first_diverge]
    v_d = vnpy_state[first_diverge]

    # diff sell/buy
    q_holdings_change_sell = qlib_state[prev_d]["holdings"] - q_d["holdings"] if prev_d else set()
    q_holdings_change_buy = q_d["holdings"] - qlib_state[prev_d]["holdings"] if prev_d else set()
    v_sells_set = {vt_to_ts(v[0]) for v in v_d["sells"]}
    v_buys_set = {vt_to_ts(v[0]) for v in v_d["buys"]}

    print(f"  qlib  sell→buy: {sorted(q_holdings_change_sell)} → {sorted(q_holdings_change_buy)}")
    print(f"  vnpy  sell→buy: {sorted(v_sells_set)} → {sorted(v_buys_set)}")

    # 4. T 日 pred_score 对比 (vnpy 推理结果)
    pred = load_pred_score(first_diverge)
    if not pred.empty:
        prev_pred = load_pred_score(prev_d) if prev_d else pd.Series(dtype=float)
        print(f"\n=== T-1 ({prev_d.date() if prev_d else 'N/A'}) pred (用于 T 日决策) ===")
        if not prev_pred.empty:
            top10 = prev_pred.sort_values(ascending=False).head(10)
            print(f"  top10: {[(t, round(prev_pred[t], 4)) for t in top10.index]}")
            # 关注 sell/buy 涉及的股票 score
            related = q_holdings_change_sell | q_holdings_change_buy | v_sells_set | v_buys_set
            for ts in sorted(related):
                if ts in prev_pred.index:
                    rank = (prev_pred.sort_values(ascending=False).index.get_loc(ts) + 1)
                    print(f"  {ts}: score={prev_pred[ts]:.6f} rank={rank}")

    # 5. T-1 持仓的具体 amount 对比 (qlib hfq close 撮合 vs vnpy raw open)
    print(f"\n=== T-1 ({prev_d.date()}) 持仓 amount 对比 (qlib hfq vs vnpy raw) ===")
    if prev_d is not None:
        for ts in sorted(qlib_state[prev_d]["holdings"]):
            q_info = qlib_state[prev_d]["details"].get(ts, {})
            q_amt = q_info.get("amount", 0)
            q_price = q_info.get("price", 0)
            v_amt = v_prev["pos_amounts"].get(ts, 0)
            v_cost = v_prev["pos_costs"].get(ts, 0)
            print(f"  {ts}:")
            print(f"    qlib amount={q_amt:.2f} price={q_price:.4f} mv={q_amt*q_price:.0f}")
            print(f"    vnpy amount={v_amt:.0f} cost={v_cost:.4f} mv={v_amt*v_cost:.0f}")
            ratio = (v_amt * v_cost) / (q_amt * q_price) if q_amt > 0 else 0
            print(f"    market_value 比 vnpy/qlib = {ratio:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
