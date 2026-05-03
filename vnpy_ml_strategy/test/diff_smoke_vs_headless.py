"""对比两份 OUT_ROOT 的同日同策略产物, 验证 smoke 与 headless 业务等价.

## 用法

```bash
# 跑 smoke
SMOKE_REPLAY_DAYS_LIMIT=5 SMOKE_LIVE_DAYS=0 \
    OUT_ROOT_OVERRIDE=/tmp/smoke_out \
    F:/Program_Home/vnpy/python.exe -u run_ml_headless_smoke.py

# 跑 headless (run_pipeline_now 一次性回放)
OUT_ROOT_OVERRIDE=/tmp/headless_out F:/Program_Home/vnpy/python.exe -u run_ml_headless.py
# 等 startup trigger 跑完所有 replay_start_date → today-1, Ctrl+C

# diff
F:/Program_Home/vnpy/python.exe scripts/diff_smoke_vs_headless.py /tmp/smoke_out /tmp/headless_out
```

返回码:
  0 = 完全等价
  1 = 有差异 (按文件分类详细列出)
  2 = 输入参数错误

## 比较什么

  selections.parquet : 7 行 ts_code 集合 (顺序无关) + score float
                       (绝对差 < 1e-9 视为等价)
  metrics.json       : n_predictions / topk_pred_mean / ic / psi_mean
                       (浮点字段绝对差 < 1e-9)
  diagnostics.json   : status / rows / live_end / model_run_id

## 不比较什么

  duration_ms       : 性能指标, 两次跑天然不同
  timestamp 字段    : 写入时刻不同
  predictions.parquet: 行数太大, 用 selections + metrics 的聚合够

## 容差

浮点 abs_diff < 1e-9 视为等价 (LightGBM 推理是确定性的, 同输入应字节一致).
若出现 > 1e-9 的差异, 通常是: (a) bundle 路径不同, (b) 推理输入数据不同
(filter snapshot / qlib bin 当时状态不同), (c) live_end 解析逻辑差异.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Tuple


FLOAT_TOL = 1e-9


def _diff_diagnostics(p_a: Path, p_b: Path) -> List[str]:
    """对比 diagnostics.json 的关键字段."""
    if not p_a.exists() and not p_b.exists():
        return []
    if not p_a.exists():
        return [f"  {p_a.name}: 仅 B 有"]
    if not p_b.exists():
        return [f"  {p_a.name}: 仅 A 有"]
    a = json.loads(p_a.read_text(encoding="utf-8"))
    b = json.loads(p_b.read_text(encoding="utf-8"))
    diffs: List[str] = []
    for key in ("status", "rows", "live_end", "model_run_id"):
        va, vb = a.get(key), b.get(key)
        if va != vb:
            diffs.append(f"  diagnostics.{key}: A={va!r} != B={vb!r}")
    return diffs


def _diff_metrics(p_a: Path, p_b: Path) -> List[str]:
    if not p_a.exists() and not p_b.exists():
        return []
    if not p_a.exists():
        return [f"  {p_a.name}: 仅 B 有"]
    if not p_b.exists():
        return [f"  {p_a.name}: 仅 A 有"]
    a = json.loads(p_a.read_text(encoding="utf-8"))
    b = json.loads(p_b.read_text(encoding="utf-8"))
    diffs: List[str] = []
    for key in ("n_predictions", "topk_pred_mean", "ic", "psi_mean"):
        va, vb = a.get(key), b.get(key)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            # NaN == NaN treated as equal
            if (va != va) and (vb != vb):
                continue
            if abs(va - vb) > FLOAT_TOL:
                diffs.append(f"  metrics.{key}: A={va!r} B={vb!r} abs_diff={abs(va-vb):.2e}")
        elif va != vb:
            diffs.append(f"  metrics.{key}: A={va!r} != B={vb!r}")
    return diffs


def _diff_selections(p_a: Path, p_b: Path) -> List[str]:
    if not p_a.exists() and not p_b.exists():
        return []
    if not p_a.exists():
        return [f"  {p_a.name}: 仅 B 有"]
    if not p_b.exists():
        return [f"  {p_a.name}: 仅 A 有"]
    import pandas as pd

    df_a = pd.read_parquet(p_a)
    df_b = pd.read_parquet(p_b)
    diffs: List[str] = []
    if len(df_a) != len(df_b):
        diffs.append(f"  selections rows: A={len(df_a)} != B={len(df_b)}")
        return diffs

    # 找 instrument 列 (常见名: ts_code / instrument / vt_symbol)
    inst_col = next(
        (c for c in ("ts_code", "instrument", "vt_symbol") if c in df_a.columns),
        None,
    )
    if inst_col is None:
        diffs.append(f"  selections: 未找到 instrument 列, A 列={list(df_a.columns)}")
        return diffs

    set_a = set(df_a[inst_col].tolist())
    set_b = set(df_b[inst_col].tolist())
    if set_a != set_b:
        only_a = set_a - set_b
        only_b = set_b - set_a
        diffs.append(f"  selections {inst_col} 集合: A 独有={sorted(only_a)} B 独有={sorted(only_b)}")
        return diffs

    # 集合相同, 比较 score 列 (若存在)
    score_col = next(
        (c for c in ("score", "pred", "y_pred") if c in df_a.columns),
        None,
    )
    if score_col is None:
        return []  # 集合一致就够了
    df_a_sorted = df_a.set_index(inst_col)[score_col].sort_index()
    df_b_sorted = df_b.set_index(inst_col)[score_col].sort_index()
    abs_diff = (df_a_sorted - df_b_sorted).abs()
    max_diff = float(abs_diff.max())
    if max_diff > FLOAT_TOL:
        diffs.append(f"  selections.{score_col} max abs_diff={max_diff:.2e} (容差 {FLOAT_TOL:.0e})")
    return diffs


def diff_strategy_day(
    out_a: Path, out_b: Path, strategy: str, day_str: str,
) -> List[str]:
    """对比单策略单日的三件套. 返回差异列表 (空 = 等价)."""
    a_dir = out_a / strategy / day_str
    b_dir = out_b / strategy / day_str
    diffs: List[str] = []
    diffs.extend(_diff_diagnostics(a_dir / "diagnostics.json", b_dir / "diagnostics.json"))
    diffs.extend(_diff_metrics(a_dir / "metrics.json", b_dir / "metrics.json"))
    diffs.extend(_diff_selections(a_dir / "selections.parquet", b_dir / "selections.parquet"))
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_a", help="OUT_ROOT A (smoke 产物)")
    parser.add_argument("out_b", help="OUT_ROOT B (headless 产物)")
    parser.add_argument(
        "--strategies", default="", help="逗号分隔策略名列表; 默认自动取两端交集"
    )
    args = parser.parse_args()

    out_a = Path(args.out_a)
    out_b = Path(args.out_b)
    if not out_a.exists() or not out_b.exists():
        print(f"FAIL: 一端目录不存在 (A={out_a.exists()} B={out_b.exists()})", file=sys.stderr)
        return 2

    if args.strategies:
        strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    else:
        strategies = sorted(
            {p.name for p in out_a.iterdir() if p.is_dir()}
            & {p.name for p in out_b.iterdir() if p.is_dir()}
        )
        if not strategies:
            print(f"FAIL: 两端无共同策略目录", file=sys.stderr)
            return 2
    print(f"对比策略: {strategies}")

    total_days = 0
    total_diff_days = 0
    all_diffs: List[Tuple[str, str, List[str]]] = []  # (strategy, day, diffs)

    for strategy in strategies:
        a_strat = out_a / strategy
        b_strat = out_b / strategy
        days_a = {p.name for p in a_strat.iterdir() if p.is_dir() and p.name.isdigit()}
        days_b = {p.name for p in b_strat.iterdir() if p.is_dir() and p.name.isdigit()}
        common_days = sorted(days_a & days_b)
        only_a_days = sorted(days_a - days_b)
        only_b_days = sorted(days_b - days_a)
        if only_a_days:
            print(f"  [{strategy}] 仅 A 有的日期 ({len(only_a_days)}): {only_a_days[:5]}{'...' if len(only_a_days) > 5 else ''}")
        if only_b_days:
            print(f"  [{strategy}] 仅 B 有的日期 ({len(only_b_days)}): {only_b_days[:5]}{'...' if len(only_b_days) > 5 else ''}")

        for day_str in common_days:
            total_days += 1
            diffs = diff_strategy_day(out_a, out_b, strategy, day_str)
            if diffs:
                total_diff_days += 1
                all_diffs.append((strategy, day_str, diffs))

    print(f"\n=== 汇总 ===")
    print(f"对比日期数: {total_days}, 有差异日期: {total_diff_days}")
    if total_diff_days == 0:
        print("✓ 全部等价 (浮点容差 1e-9)")
        return 0

    print(f"\n=== 差异详情 (前 20 条) ===")
    for strategy, day_str, diffs in all_diffs[:20]:
        print(f"[{strategy}/{day_str}]")
        for d in diffs:
            print(d)
    if len(all_diffs) > 20:
        print(f"... 还有 {len(all_diffs) - 20} 个有差异的日期未列出")
    return 1


if __name__ == "__main__":
    sys.exit(main())
