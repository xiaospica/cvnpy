"""[DEPRECATED · A1/B2 解耦] 一次性脚本：从 D:/ml_output/{strategy}/{yyyymmdd}/ 回填
ml_metric_snapshots + ml_prediction_daily 到 mlearnweb.db。

⚠️ 本脚本已废弃 (commit A1 Step 1):
  - vnpy 不再直接写 mlearnweb.db 的 ml_metric_snapshots / ml_prediction_daily
  - 这两张表的回填责任已转移到 mlearnweb 端 historical_metrics_sync_service,
    该 service 每 5 分钟从 vnpy_webtrader /api/v1/ml/strategies/{name}/metrics?days=30
    拉历史 + UPSERT 本地, 无需手工脚本

如果确实有 mlearnweb retention bug 导致历史丢失, 应该:
  1. 提高 mlearnweb 的 retention_days (或修复 bug)
  2. 调 mlearnweb 内部的 historical_metrics_sync_service 全量同步触发器

详见 docs/deployment_a1_p21_plan.md §一.1 Step 1.
"""
from __future__ import annotations

import sys

raise RuntimeError(
    "scripts/backfill_ml_metric_snapshots.py 已废弃 (A1/B2 解耦 Step 1, 详见 "
    "docs/deployment_a1_p21_plan.md). vnpy 不再直接写 mlearnweb.db 的 "
    "ml_metric_snapshots / ml_prediction_daily, 历史回填由 mlearnweb 端 "
    "historical_metrics_sync_service 自动负责. "
    "如确需手工触发同步, 请走 mlearnweb 后端 API."
)

# 以下代码保留作历史参考, 不会执行 (上面 raise 已退出).
# ruff: noqa
import json
from datetime import date, datetime, time as dtime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from vnpy_ml_strategy.mlearnweb_writer import write_replay_ml_metric_snapshot  # noqa: E402

OUTPUT_ROOT = Path(r"D:/ml_output")
NODE_ID = "local"
ENGINE = "MlStrategy"


def iter_replay_days(strategy_name: str):
    base = OUTPUT_ROOT / strategy_name
    if not base.exists():
        return
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if not (len(name) == 8 and name.isdigit()):
            continue
        try:
            day = datetime.strptime(name, "%Y%m%d").date()
        except ValueError:
            continue
        diag_path = d / "diagnostics.json"
        if not diag_path.exists():
            continue
        yield day, d, diag_path


def backfill_strategy(strategy_name: str) -> int:
    n_written = 0
    n_skipped = 0
    for day, day_dir, diag_path in iter_replay_days(strategy_name):
        try:
            diag = json.loads(diag_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  [skip] {day} diag 读取失败: {exc}")
            n_skipped += 1
            continue

        if diag.get("status") not in ("ok", "completed"):
            n_skipped += 1
            continue

        metrics_path = day_dir / "metrics.json"
        metrics: dict = {}
        if metrics_path.exists():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except Exception:
                metrics = {}
        metrics.setdefault("model_run_id", diag.get("model_run_id"))
        metrics.setdefault("core_version", diag.get("core_version"))
        metrics.setdefault("status", diag.get("status"))
        metrics.setdefault("n_predictions", diag.get("rows"))

        topk_list = []
        sel_path = day_dir / "selections.parquet"
        if sel_path.exists():
            try:
                sel = pd.read_parquet(sel_path)
                sel_reset = sel.reset_index() if not sel.empty else sel
                for i, (_, r) in enumerate(sel_reset.iterrows()):
                    topk_list.append({
                        "rank": i + 1,
                        "instrument": str(r.get("instrument", "")),
                        "score": float(r["score"]) if "score" in r and r["score"] is not None else None,
                    })
            except Exception as exc:
                print(f"  [warn] {day} selections 读取失败: {exc}")

        topk_summary = {
            "topk": topk_list,
            "score_histogram": metrics.get("score_histogram") or [],
            "n_symbols": diag.get("rows", 0),
            "pred_mean": metrics.get("pred_mean"),
            "pred_std": metrics.get("pred_std"),
            "model_run_id": diag.get("model_run_id"),
            "status": diag.get("status"),
        }

        trade_dt = datetime.combine(day, dtime(15, 0, 0))
        try:
            ok = write_replay_ml_metric_snapshot(
                node_id=NODE_ID,
                engine=ENGINE,
                strategy_name=strategy_name,
                trade_date=trade_dt,
                metrics=metrics,
                topk_summary=topk_summary,
            )
            if ok:
                n_written += 1
            else:
                n_skipped += 1
        except Exception as exc:
            print(f"  [err] {day} 写入失败: {exc}")
            n_skipped += 1

    return n_written


if __name__ == "__main__":
    strategies = sys.argv[1:] or ["csi300_lgb_headless"]
    for s in strategies:
        print(f"=== {s} ===")
        n = backfill_strategy(s)
        print(f"  写入 {n} 行")
