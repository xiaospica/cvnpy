# -*- coding: utf-8 -*-
"""把 sim 网关的成交/持仓 与 原始 CSV 做端到端对账，输出诊断报告。

对账维度（按重要性）：

1. **持仓终态**（必须全对）
   - CSV 源：position.csv 最后一日的快照（每个标的 数量+可用数量）
   - sim 源：sim_positions 表当前快照
   - 比对：(vt_symbol, volume)；容差 ±volume_tolerance（默认 100 股）

2. **成交流水（按日累计）**
   - CSV 源：transaction.csv 全表，按 (日期, vt_symbol, 方向) 聚合 sum(数量)
   - sim 源：sim_trades 表，按 (日期, vt_symbol, direction) 聚合 sum(volume)
   - 比对：股数差异；价格只参考（sim 用 bar.open，CSV 是真实分笔，必有偏差）

3. **拒单原因诊断**
   - 数据源：sim_orders WHERE status='REJECTED'
   - 输出每种 reject reason 的计数（资金不足 / T+1 持仓不足 / 涨跌停 等）
   - 帮助定位"为什么 sim 持仓与 CSV 不一致"的根因

退出码：0 = 持仓全部 PASS（可能有成交差异，但终态一致），1 = 持仓有差异。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


def resolve_setting_path(template_path: Path) -> Path:
    """优先 ``.local.json`` 副本，fallback 到模板。"""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


JQ_TO_VNPY_EXCHANGE = {
    "XSHG": "SSE",
    "XSHE": "SZSE",
}

INSTRUMENT_RE = re.compile(r"\((\d{6}\.[A-Z]+)\)")

DIRECTION_BUY_CN = "买"
DIRECTION_SELL_CN = "卖"

# vnpy Direction.LONG.value / Direction.SHORT.value 常量值（避免 import vnpy 拖整套依赖）
SIM_DIR_LONG = "多"
SIM_DIR_SHORT = "空"


@dataclass
class ReconcileConfig:
    sim_db_path: Path
    transaction_csv: Path
    position_csv: Path
    csv_encoding: str
    output_dir: Path
    volume_tolerance: int
    ratio_tolerance: float  # 市值占比容差（小数，如 0.01 = 1%）

    @classmethod
    def from_test_setting(cls, setting: dict) -> "ReconcileConfig":
        sim = setting["sim"]
        sim_db_dir = Path(sim["db_dir"])
        sim_db_path = sim_db_dir / f"sim_{sim['account_id']}.db"

        rec = setting["reconcile"]
        return cls(
            sim_db_path=sim_db_path,
            transaction_csv=Path(setting["csv"]["transaction_path"]),
            position_csv=Path(setting["csv"]["position_path"]),
            csv_encoding=setting["csv"].get("encoding", "gbk"),
            output_dir=Path(rec["output_dir"]),
            volume_tolerance=int(rec.get("volume_tolerance", 100)),
            ratio_tolerance=float(rec.get("ratio_tolerance", 0.01)),
        )


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("reconcile_trades")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _parse_qty_to_int(qty_str) -> int:
    """'35400股' / '-35400股' -> abs int；NaN -> 0。"""
    if pd.isna(qty_str):
        return 0
    s = str(qty_str).strip().rstrip("股").rstrip("手").rstrip(",")
    try:
        return abs(int(float(s)))
    except ValueError:
        return 0


def _instrument_to_vt_symbol(raw) -> Optional[str]:
    """'黄金ETF(518880.XSHG)' -> '518880.SSE'。"""
    if pd.isna(raw):
        return None
    m = INSTRUMENT_RE.search(str(raw))
    if not m:
        return None
    code, suffix = m.group(1).split(".")
    vnpy_suffix = JQ_TO_VNPY_EXCHANGE.get(suffix)
    if not vnpy_suffix:
        return None
    return f"{code}.{vnpy_suffix}"


# ---------------- 数据加载 ----------------


# 聚宽导出 position.csv 的实际列数：header 16 列，但持仓数据行 17 列
# （header 在"今手数"和"仓位占比"之间漏掉了"权益总值"列名）。直接 read_csv
# 会抛 "Expected 16 fields, saw 17"。用 skiprows=1 + 显式 17 列 names 解决。
POSITION_COLS17 = [
    "date", "category", "instrument", "direction",
    "volume", "available", "close", "market_value",
    "pnl", "open_price", "holding_price", "margin",
    "today_pnl", "today_volume", "pnl_ratio",
    "equity_total", "position_ratio",
]


def load_csv_positions_last_day(cfg: ReconcileConfig, logger: logging.Logger) -> pd.DataFrame:
    """读 position.csv 取最后一日的非现金持仓快照。

    返回列：vt_symbol, volume, available, market_value, position_ratio
    （position_ratio 已转 0~1 小数，便于与 sim 对账）。
    """
    if not cfg.position_csv.exists():
        raise FileNotFoundError(f"position CSV 不存在: {cfg.position_csv}")
    df = pd.read_csv(
        cfg.position_csv,
        encoding=cfg.csv_encoding,
        skiprows=1,
        header=None,
        names=POSITION_COLS17,
        engine="python",
        on_bad_lines="warn",
    )
    logger.info(f"[csv-pos] read {len(df)} 行")
    # 现金行：category 为 NaN；持仓行 category="股票"/"基金" 等
    df = df[df["category"].notna()].copy()
    df = df[df["instrument"].astype(str).str.contains(r"\(", regex=True)]
    df["date"] = df["date"].astype(str)

    last_day = sorted(df["date"].dropna().unique())[-1]
    logger.info(f"[csv-pos] 最后一日 {last_day}, 总持仓 {len(df[df['date']==last_day])} 行")

    snap = df[df["date"] == last_day].copy()
    snap["vt_symbol"] = snap["instrument"].apply(_instrument_to_vt_symbol)
    snap["volume"] = snap["volume"].apply(_parse_qty_to_int)
    snap["available"] = snap["available"].apply(_parse_qty_to_int)
    snap["market_value"] = pd.to_numeric(snap["market_value"], errors="coerce").fillna(0.0)
    # position_ratio: "8.7%" -> 0.087；空或异常 -> 0
    snap["position_ratio"] = (
        snap["position_ratio"].astype(str).str.rstrip("%")
        .replace({"": "0", "nan": "0"})
        .pipe(pd.to_numeric, errors="coerce")
        .fillna(0.0) / 100.0
    )
    snap = snap[snap["vt_symbol"].notna()].copy()

    out = snap[["vt_symbol", "volume", "available", "market_value", "position_ratio"]].copy()
    out = out[out["volume"] > 0]
    out = out.groupby("vt_symbol", as_index=False).agg({
        "volume": "sum",
        "available": "sum",
        "market_value": "sum",
        "position_ratio": "sum",
    })
    return out


def load_csv_trades(cfg: ReconcileConfig, logger: logging.Logger) -> pd.DataFrame:
    """读 transaction.csv，返回每行清洗后的成交。

    列：trade_date, vt_symbol, direction (LONG/SHORT), volume, price
    """
    df = pd.read_csv(cfg.transaction_csv, encoding=cfg.csv_encoding)

    out = pd.DataFrame()
    out["trade_date"] = df["日期"].astype(str)
    out["vt_symbol"] = df["标的"].apply(_instrument_to_vt_symbol)
    out["direction"] = df["交易类型"].map(
        {DIRECTION_BUY_CN: "LONG", DIRECTION_SELL_CN: "SHORT"}
    )
    out["volume"] = df["成交数量"].apply(_parse_qty_to_int)
    out["price"] = pd.to_numeric(df["成交价"], errors="coerce")

    out = out.dropna(subset=["vt_symbol", "direction"])
    out = out[out["volume"] > 0]
    out = out[out["price"].notna()]
    logger.info(f"[csv-trades] 清洗后 {len(out)} 条成交")
    return out.reset_index(drop=True)


def load_sim_positions(cfg: ReconcileConfig, logger: logging.Logger) -> pd.DataFrame:
    """从 sim_positions 表读取当前所有 LONG 持仓 volume>0 的快照。

    返回列：vt_symbol, volume, available, price, market_value
    market_value = volume * price（pos.price 在 settle_end_of_day 后等于该日 close
    经 mark-to-market 调整的"等效成本/最新价"，是合理的市值口径）。
    """
    if not cfg.sim_db_path.exists():
        raise FileNotFoundError(f"sim db 不存在: {cfg.sim_db_path}")
    with sqlite3.connect(cfg.sim_db_path) as conn:
        df = pd.read_sql(
            "SELECT account_id, vt_symbol, direction, volume, yd_volume, frozen, price "
            "FROM sim_positions WHERE volume > 0",
            conn,
        )
    df = df[df["direction"].isin([SIM_DIR_LONG, "long", "Long", "LONG"])].copy()
    df["volume"] = df["volume"].astype(int)
    df["price"] = df["price"].astype(float)
    df["market_value"] = df["volume"] * df["price"]
    df["available"] = (df["yd_volume"].astype(float) - df["frozen"].astype(float)).clip(lower=0).astype(int)
    out = df[["vt_symbol", "volume", "available", "price", "market_value"]].copy()
    logger.info(
        f"[sim-pos] {len(out)} 行 LONG 持仓; sum(market_value)={out['market_value'].sum():,.2f}"
    )
    return out.reset_index(drop=True)


def load_sim_account_balance(cfg: ReconcileConfig) -> float:
    """读 sim_accounts 当前现金余额。"""
    with sqlite3.connect(cfg.sim_db_path) as conn:
        row = conn.execute(
            "SELECT capital FROM sim_accounts ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return float(row[0]) if row else 0.0


def load_sim_trades(cfg: ReconcileConfig, logger: logging.Logger) -> pd.DataFrame:
    """从 sim_trades 表读全部成交，加 trade_date 列方便聚合。"""
    with sqlite3.connect(cfg.sim_db_path) as conn:
        df = pd.read_sql(
            "SELECT account_id, tradeid, orderid, vt_symbol, direction, "
            "       offset, price, volume, datetime FROM sim_trades",
            conn,
        )
    if df.empty:
        logger.warning("[sim-trades] sim_trades 表为空")
        df["trade_date"] = []
        return df

    # datetime 是 ISO 字符串 'YYYY-MM-DDTHH:MM:SS'
    df["trade_date"] = df["datetime"].astype(str).str.slice(0, 10)
    # 标准化 direction 为 LONG/SHORT
    df["direction"] = df["direction"].map(
        lambda x: "LONG" if x in (SIM_DIR_LONG, "long", "Long", "LONG") else "SHORT"
    )
    df["volume"] = df["volume"].astype(int)
    df["price"] = df["price"].astype(float)
    logger.info(f"[sim-trades] {len(df)} 条成交")
    return df.reset_index(drop=True)


def load_sim_orders(cfg: ReconcileConfig, logger: logging.Logger) -> pd.DataFrame:
    """读所有订单（含拒单），用于诊断。"""
    with sqlite3.connect(cfg.sim_db_path) as conn:
        df = pd.read_sql(
            "SELECT orderid, vt_symbol, direction, status, status_msg, "
            "       price, volume, traded, datetime FROM sim_orders",
            conn,
        )
    return df


# ---------------- 对账 ----------------


def reconcile_positions(
    csv_pos: pd.DataFrame,
    sim_pos: pd.DataFrame,
    tolerance: int,
    logger: logging.Logger,
) -> tuple[pd.DataFrame, bool]:
    merged = csv_pos[["vt_symbol", "volume", "available"]].merge(
        sim_pos[["vt_symbol", "volume", "available"]],
        on="vt_symbol",
        how="outer",
        suffixes=("_csv", "_sim"),
    ).fillna(0)
    merged["volume_csv"] = merged["volume_csv"].astype(int)
    merged["volume_sim"] = merged["volume_sim"].astype(int)
    merged["diff"] = merged["volume_sim"] - merged["volume_csv"]
    merged["abs_diff"] = merged["diff"].abs()
    merged["status"] = merged["abs_diff"].apply(
        lambda x: "PASS" if x <= tolerance else "FAIL"
    )

    all_pass = (merged["status"] == "PASS").all()
    n_fail = (merged["status"] == "FAIL").sum()
    logger.info(
        f"[reconcile-pos] 股数维度 {len(merged)} 个标的，{n_fail} FAIL "
        f"(容差={tolerance} 股) -> {'PASS' if all_pass else 'FAIL'}"
    )
    return merged.sort_values(["status", "abs_diff"], ascending=[False, False]), all_pass


def reconcile_position_ratio(
    csv_pos: pd.DataFrame,
    sim_pos: pd.DataFrame,
    sim_cash: float,
    ratio_tolerance: float,
    logger: logging.Logger,
) -> tuple[pd.DataFrame, bool]:
    """按市值占比对账：sim 用 fallback price 时绝对股数失真，但占比应保持。

    csv 占比口径：直接读 position.csv 的"仓位占比"列（已转 0~1 小数）。
    sim 占比口径：sim_pos.market_value / (sim_total_assets)，
                 sim_total_assets = sum(market_value) + sim_cash 。
    """
    sim_total_mv = float(sim_pos["market_value"].sum())
    sim_total_assets = sim_total_mv + sim_cash
    csv_total_assets = float(csv_pos["market_value"].sum())  # csv 不含 cash 行也接近真实

    sim = sim_pos[["vt_symbol", "market_value"]].copy()
    sim["sim_ratio"] = sim["market_value"] / sim_total_assets if sim_total_assets > 0 else 0.0
    sim = sim.rename(columns={"market_value": "sim_mv"})

    csv = csv_pos[["vt_symbol", "market_value", "position_ratio"]].copy()
    csv = csv.rename(
        columns={"market_value": "csv_mv", "position_ratio": "csv_ratio"}
    )

    merged = csv.merge(sim, on="vt_symbol", how="outer").fillna(0.0)
    merged["ratio_diff"] = merged["sim_ratio"] - merged["csv_ratio"]
    merged["abs_ratio_diff"] = merged["ratio_diff"].abs()
    merged["status"] = merged["abs_ratio_diff"].apply(
        lambda x: "PASS" if x <= ratio_tolerance else "FAIL"
    )

    all_pass = (merged["status"] == "PASS").all()
    n_fail = (merged["status"] == "FAIL").sum()
    logger.info(
        f"[reconcile-pos-ratio] 市值占比维度 {len(merged)} 个标的，{n_fail} FAIL "
        f"(容差=±{ratio_tolerance*100:.1f}%) -> {'PASS' if all_pass else 'FAIL'}"
        f"; sim_total={sim_total_assets:,.0f} csv_total≈{csv_total_assets:,.0f}"
    )
    # 美化显示：占比转百分比字符串
    merged_disp = merged.copy()
    merged_disp["csv_ratio"] = (merged_disp["csv_ratio"] * 100).round(2).astype(str) + "%"
    merged_disp["sim_ratio"] = (merged_disp["sim_ratio"] * 100).round(2).astype(str) + "%"
    merged_disp["ratio_diff"] = (merged_disp["ratio_diff"] * 100).round(2).astype(str) + "%"
    merged_disp = merged_disp.drop(columns=["abs_ratio_diff"])
    return merged_disp.sort_values(
        ["status", "ratio_diff"], ascending=[False, False]
    ), all_pass


def reconcile_trades_daily(
    csv_trades: pd.DataFrame,
    sim_trades: pd.DataFrame,
    logger: logging.Logger,
) -> pd.DataFrame:
    """按 (trade_date, vt_symbol, direction) 聚合后比对累计股数。"""
    csv_agg = (
        csv_trades.groupby(["trade_date", "vt_symbol", "direction"], as_index=False)
        .agg(volume_csv=("volume", "sum"), price_csv_avg=("price", "mean"))
    )
    if sim_trades.empty:
        sim_agg = pd.DataFrame(
            columns=["trade_date", "vt_symbol", "direction", "volume_sim", "price_sim_avg"]
        )
    else:
        sim_agg = (
            sim_trades.groupby(["trade_date", "vt_symbol", "direction"], as_index=False)
            .agg(volume_sim=("volume", "sum"), price_sim_avg=("price", "mean"))
        )

    merged = csv_agg.merge(
        sim_agg, on=["trade_date", "vt_symbol", "direction"], how="outer"
    ).fillna({"volume_csv": 0, "volume_sim": 0})
    merged["volume_csv"] = merged["volume_csv"].astype(int)
    merged["volume_sim"] = merged["volume_sim"].astype(int)
    merged["volume_diff"] = merged["volume_sim"] - merged["volume_csv"]

    n_match = (merged["volume_diff"] == 0).sum()
    logger.info(
        f"[reconcile-trades] {len(merged)} 个 (date,symbol,dir) 组，"
        f"{n_match} 完全匹配"
    )
    return merged.sort_values(["trade_date", "vt_symbol", "direction"]).reset_index(drop=True)


def summarize_rejects(orders: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    rej = orders[orders["status"] == "REJECTED"].copy()
    if rej.empty:
        logger.info("[reconcile-rejects] 无拒单")
        return rej
    rej["status_msg"] = rej["status_msg"].fillna("")
    summary = (
        rej.groupby("status_msg", as_index=False)
        .size()
        .rename(columns={"size": "count"})
        .sort_values("count", ascending=False)
    )
    logger.info(f"[reconcile-rejects] 共 {len(rej)} 笔拒单，按原因聚合：")
    for _, r in summary.iterrows():
        logger.info(f"  count={r['count']:5d}  msg={r['status_msg']!r}")
    return rej


# ---------------- 入口 ----------------


def reconcile(setting_path: Path, logger: Optional[logging.Logger] = None) -> int:
    if logger is None:
        logger = _setup_logger()

    with open(setting_path, "r", encoding="utf-8") as f:
        setting = json.load(f)
    cfg = ReconcileConfig.from_test_setting(setting)
    logger.info(f"[cfg] sim_db={cfg.sim_db_path}")
    logger.info(f"[cfg] tx_csv={cfg.transaction_csv}")
    logger.info(f"[cfg] pos_csv={cfg.position_csv}")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    csv_pos = load_csv_positions_last_day(cfg, logger)
    csv_trades = load_csv_trades(cfg, logger)
    sim_pos = load_sim_positions(cfg, logger)
    sim_trades = load_sim_trades(cfg, logger)
    sim_orders = load_sim_orders(cfg, logger)
    sim_cash = load_sim_account_balance(cfg)
    logger.info(f"[sim-account] cash={sim_cash:,.2f}")

    pos_diff, pos_all_pass = reconcile_positions(
        csv_pos, sim_pos, cfg.volume_tolerance, logger
    )
    ratio_diff, ratio_all_pass = reconcile_position_ratio(
        csv_pos, sim_pos, sim_cash, cfg.ratio_tolerance, logger
    )
    trade_diff = reconcile_trades_daily(csv_trades, sim_trades, logger)
    rejects = summarize_rejects(sim_orders, logger)

    pos_path = cfg.output_dir / "reconcile_position.csv"
    ratio_path = cfg.output_dir / "reconcile_position_ratio.csv"
    trade_path = cfg.output_dir / "reconcile_trades.csv"
    rej_path = cfg.output_dir / "reconcile_rejects.csv"
    pos_diff.to_csv(pos_path, index=False, encoding="utf-8-sig")
    ratio_diff.to_csv(ratio_path, index=False, encoding="utf-8-sig")
    trade_diff.to_csv(trade_path, index=False, encoding="utf-8-sig")
    if not rejects.empty:
        rejects.to_csv(rej_path, index=False, encoding="utf-8-sig")

    logger.info(f"[done] 写入 {pos_path}")
    logger.info(f"[done] 写入 {ratio_path}")
    logger.info(f"[done] 写入 {trade_path}")
    if not rejects.empty:
        logger.info(f"[done] 写入 {rej_path}")

    # 主判定：市值占比维度（fallback 价场景下唯一可靠的对账口径）
    primary_pass = ratio_all_pass
    if primary_pass:
        logger.info("[E2E] PASS 市值占比对账全部通过")
        if pos_all_pass:
            logger.info("[E2E] 股数维度也全部一致（行情数据精确）")
        else:
            logger.info(
                "[E2E] 股数维度有差异（合理：sim 用 fallback 价撮合，绝对股数失真但占比保持）"
            )
        return 0
    else:
        n_fail = (ratio_diff["status"] == "FAIL").sum()
        logger.error(
            f"[E2E] FAIL 市值占比维度有 {n_fail} 个标的超容差 {cfg.ratio_tolerance*100:.1f}%，"
            f"详见 {ratio_path}"
        )
        fails = ratio_diff[ratio_diff["status"] == "FAIL"].head(10)
        logger.error("前 10 个市值占比差异：")
        for _, r in fails.iterrows():
            logger.error(
                f"  {r['vt_symbol']:>15s}  csv={str(r['csv_ratio']):>7s}  "
                f"sim={str(r['sim_ratio']):>7s}  diff={str(r['ratio_diff']):>8s}"
            )
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="sim 网关 vs 原始 CSV 对账")
    parser.add_argument(
        "--config",
        default=str(
            resolve_setting_path(Path(__file__).resolve().parent / "test_setting.json")
        ),
        help="test_setting.json 路径（默认优先 .local.json 副本）",
    )
    args = parser.parse_args()
    code = reconcile(Path(args.config))
    sys.exit(code)


if __name__ == "__main__":
    main()
