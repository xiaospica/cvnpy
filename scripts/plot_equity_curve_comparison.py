"""画 vnpy 回放权益曲线 vs qlib backtest 复利累积收益率对比图.

输出: docs/equity_curve_comparison.png

数据源:
  - vnpy: mlearnweb db strategy_equity_snapshots replay_settle
  - qlib: report_normal_1day.pkl (account 列)
"""
from __future__ import annotations

import pickle
import sqlite3
from datetime import datetime
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

QLIB_BT_REPORT = Path(r"C:/Users/richard/AppData/Local/Temp/qlib_d_backtest/report_normal_1day.pkl")
MLEARNWEB_DB = Path(r"f:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db")
OUT_DIR = Path(r"f:/Quant/vnpy/vnpy_strategy_dev/docs")
INIT_CASH = 1_000_000.0


def load_qlib_curve():
    report = pickle.load(open(QLIB_BT_REPORT, "rb"))
    series = report["account"].copy()
    series.index = pd.to_datetime(series.index)
    return series


def load_vnpy_curve():
    conn = sqlite3.connect(str(MLEARNWEB_DB))
    cur = conn.cursor()
    cur.execute(
        """SELECT ts, strategy_value FROM strategy_equity_snapshots
           WHERE strategy_name='csi300_lgb_headless' AND source_label='replay_settle'
           ORDER BY ts ASC"""
    )
    rows = cur.fetchall()
    conn.close()
    data = []
    for ts_str, val in rows:
        try:
            d = pd.Timestamp(datetime.fromisoformat(ts_str).date())
            data.append((d, float(val) if val else 0.0))
        except Exception:
            continue
    return pd.Series({d: v for d, v in data}).sort_index()


def main():
    q = load_qlib_curve()
    v = load_vnpy_curve()
    overlap = sorted(set(q.index) & set(v.index))
    q_aligned = q.loc[overlap]
    v_aligned = v.loc[overlap]

    q_cumret = q_aligned / INIT_CASH - 1
    v_cumret = v_aligned / INIT_CASH - 1
    diff = v_cumret - q_cumret

    # daily return for return_diff plot
    q_ret = q_aligned.pct_change().fillna(0)
    v_ret = v_aligned.pct_change().fillna(0)
    ret_diff = v_ret - q_ret

    plt.style.use("dark_background")
    fig, axes = plt.subplots(3, 1, figsize=(14, 11), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1.2]})

    # 1. 累积收益率曲线
    ax = axes[0]
    ax.plot(q_cumret.index, q_cumret.values * 100, label="qlib backtest", linewidth=2, color="#3B82F6")
    ax.plot(v_cumret.index, v_cumret.values * 100, label="vnpy 回放", linewidth=2, color="#F59E0B")
    ax.axhline(0, color="gray", alpha=0.4, linewidth=0.5)
    ax.set_ylabel("累积收益率 (%)", fontsize=11)
    ax.set_title(
        f"vnpy 回放 vs qlib backtest 累积收益率对比 — 同 D:/vnpy_data 数据源 / 同 bundle / qlib deal_price=$open\n"
        f"重叠 {len(overlap)} 个交易日 ({overlap[0].date()} ~ {overlap[-1].date()})",
        fontsize=12,
    )
    ax.legend(loc="upper left", fontsize=10)
    ax.grid(True, alpha=0.2)

    # 标注最终值
    ax.annotate(
        f"qlib 终值 {q_cumret.iloc[-1]*100:+.2f}%",
        xy=(q_cumret.index[-1], q_cumret.iloc[-1] * 100),
        xytext=(8, 8), textcoords="offset points",
        color="#3B82F6", fontsize=9,
    )
    ax.annotate(
        f"vnpy 终值 {v_cumret.iloc[-1]*100:+.2f}%",
        xy=(v_cumret.index[-1], v_cumret.iloc[-1] * 100),
        xytext=(8, -16), textcoords="offset points",
        color="#F59E0B", fontsize=9,
    )

    # 2. cumret 偏差 (vnpy - qlib)
    ax = axes[1]
    ax.fill_between(diff.index, 0, diff.values * 100, color="#EF4444", alpha=0.4)
    ax.plot(diff.index, diff.values * 100, color="#EF4444", linewidth=1.2)
    ax.axhline(0, color="gray", alpha=0.4, linewidth=0.5)
    ax.set_ylabel("cumret 偏差\n(vnpy - qlib) %", fontsize=10)
    ax.grid(True, alpha=0.2)
    max_diff = diff.abs().max()
    ax.text(0.01, 0.85, f"max abs偏差: {max_diff*100:.3f}%  最终: {diff.iloc[-1]*100:+.3f}%",
            transform=ax.transAxes, fontsize=9, color="#EF4444",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="black", alpha=0.6, edgecolor="#EF4444"))

    # 3. daily return 偏差
    ax = axes[2]
    ax.bar(ret_diff.index, ret_diff.values * 100, color="#10B981", width=0.7, alpha=0.7)
    ax.axhline(0, color="gray", alpha=0.4, linewidth=0.5)
    ax.set_ylabel("日 return 偏差\n(vnpy - qlib) %", fontsize=10)
    ax.set_xlabel("日期", fontsize=11)
    ax.grid(True, alpha=0.2)
    avg_ret_diff = ret_diff.abs().mean()
    max_ret_diff = ret_diff.abs().max()
    pearson = q_ret.corr(v_ret)
    ax.text(0.01, 0.85,
            f"avg abs: {avg_ret_diff*100:.3f}%  max: {max_ret_diff*100:.3f}%  pearson: {pearson:.4f}",
            transform=ax.transAxes, fontsize=9, color="#10B981",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="black", alpha=0.6, edgecolor="#10B981"))

    # X 轴日期格式
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")

    plt.tight_layout()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "equity_curve_comparison.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#0A0E1A")
    print(f"saved: {out_path}")


if __name__ == "__main__":
    # 中文字体
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    main()
