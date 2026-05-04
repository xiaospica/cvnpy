"""画 vnpy 回放权益曲线 vs qlib backtest 复利累积收益率对比图.

输出: vnpy_ml_strategy/test/result/equity_curve_comparison_{strategy_name}.png

数据源:
  - vnpy: mlearnweb db strategy_equity_snapshots replay_settle
  - qlib: {QLIB_BT_BASE}/{strategy_name}/report_normal_1day.pkl (account 列)

⚠️ 每个 strategy 读自己的 ground truth 子目录 — 之前所有策略读同一份
``report_normal_1day.pkl``, 不同 bundle 拿到相同 qlib 曲线虚假对比, 已修复.
对应的 ground truth 必须先跑:
    generate_qlib_ground_truth.py --strategy-name {strategy_name}
+ env BUNDLE_DIR=...

CLI:
  python plot_equity_curve_comparison.py [strategy_name]
  默认 strategy_name=csi300_lgb_headless.
"""
from __future__ import annotations

import argparse
import pickle
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]  # vnpy_strategy_dev
sys.path.insert(0, str(_ROOT / "vendor" / "qlib_strategy_core"))  # qlib (unpickle report)

# qlib ground truth 按 strategy_name 隔离的根目录 (与
# generate_qlib_ground_truth.py OUT_DIR_BASE 同源).
QLIB_BT_BASE = Path(r"C:/Users/richard/AppData/Local/Temp/qlib_d_backtest")
MLEARNWEB_DB = Path(r"f:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db")
OUT_DIR = Path(__file__).resolve().parent / "result"
INIT_CASH = 1_000_000.0


def _qlib_report_path(strategy_name: str) -> Path:
    """每个 strategy 独立的 ground truth 路径."""
    return QLIB_BT_BASE / strategy_name / "report_normal_1day.pkl"


def load_qlib_curve(strategy_name: str):
    report_path = _qlib_report_path(strategy_name)
    if not report_path.exists():
        raise FileNotFoundError(
            f"qlib ground truth 不存在: {report_path}\n"
            f"先跑 generate_qlib_ground_truth.py --strategy-name {strategy_name} "
            f"(配 env BUNDLE_DIR 指向对应 bundle)."
        )
    report = pickle.load(open(report_path, "rb"))
    series = report["account"].copy()
    series.index = pd.to_datetime(series.index)
    return series


def load_vnpy_curve(strategy_name: str):
    conn = sqlite3.connect(str(MLEARNWEB_DB))
    cur = conn.cursor()
    cur.execute(
        """SELECT ts, strategy_value FROM strategy_equity_snapshots
           WHERE strategy_name=? AND source_label='replay_settle'
           ORDER BY ts ASC""",
        (strategy_name,),
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


def main(strategy_name: str = "csi300_lgb_headless"):
    q = load_qlib_curve(strategy_name)
    v = load_vnpy_curve(strategy_name)
    if v.empty:
        raise SystemExit(
            f"vnpy 曲线为空: strategy_name='{strategy_name}' 在 strategy_equity_snapshots "
            f"中无 replay_settle 记录。检查 vnpy 实盘进程是否已用此 strategy_name 跑完回放。"
        )
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
        f"vnpy 回放 vs qlib backtest 累积收益率对比 — strategy={strategy_name}\n"
        f"同 D:/vnpy_data 数据源 / 同 bundle / qlib deal_price=$open / 重叠 {len(overlap)} 个交易日 ({overlap[0].date()} ~ {overlap[-1].date()})",
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
    # 文件名按 strategy_name 强隔离 — 不再有"默认策略名 = equity_curve_comparison.png"
    # 的隐式约定 (那是 ground truth 写死单一目录时代的兼容物). 现在每个策略名 1:1 输出.
    out_path = OUT_DIR / f"equity_curve_comparison_{strategy_name}.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#0A0E1A")
    print(f"saved: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "strategy_name",
        nargs="?",
        default="csi300_lgb_headless",
        help="strategy name (default: csi300_lgb_headless)",
    )
    args = parser.parse_args()

    # 中文字体
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    main(args.strategy_name)
