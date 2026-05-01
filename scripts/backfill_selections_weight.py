"""一次性回填 selections.parquet 的 weight 字段。

Bug 2 (template.py:486) 历史 bug：
  weight = 1.0 / len(sel_df)   # 漏乘 risk_degree → 等权 1/7 = 14.29%
修复后:
  weight = risk_degree / len(sel_df)   # 正确 0.95/7 = 13.57%

D:/ml_output/csi300_lgb_headless/{yyyymmdd}/selections.parquet 86+ 个文件需要回填。
"""
from __future__ import annotations
import sys
from pathlib import Path

import pandas as pd

OUTPUT_ROOT = Path(r"D:/ml_output")
RISK_DEGREE = 0.95


def backfill(strategy_name: str) -> int:
    base = OUTPUT_ROOT / strategy_name
    if not base.exists():
        print(f"  no dir: {base}")
        return 0
    n = 0
    for day_dir in sorted(base.iterdir()):
        if not day_dir.is_dir() or not day_dir.name.isdigit():
            continue
        sel = day_dir / "selections.parquet"
        if not sel.exists():
            continue
        try:
            df = pd.read_parquet(sel)
        except Exception as e:
            print(f"  [skip] {day_dir.name}: read err {e}")
            continue
        if "weight" not in df.columns or df.empty:
            continue
        new_w = RISK_DEGREE / len(df)
        old_w = float(df["weight"].iloc[0]) if len(df) > 0 else None
        if old_w is not None and abs(old_w - new_w) < 1e-6:
            continue  # 已经对了
        df["weight"] = new_w
        # atomic write
        tmp = sel.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(sel)
        n += 1
    return n


if __name__ == "__main__":
    strategies = sys.argv[1:] or ["csi300_lgb_headless"]
    for s in strategies:
        print(f"=== {s} ===")
        n = backfill(s)
        print(f"  回填 {n} 个 selections.parquet (weight: 1/topk → risk_degree/topk)")
