"""Result persistence — 主进程侧 selections / orders 落盘.

目录约定 (与 vnpy 架构师方案第 222-234 行对齐):

    {output_root}/{strategy_name}/{yyyymmdd}/
      predictions.parquet    ← 子进程写
      metrics.json           ← 子进程写
      diagnostics.json       ← 子进程写
      selections.parquet     ← 主进程写 (本模块)
      orders.jsonl           ← 主进程写 (本模块)
      latest.json            ← Phase 2.5 publish_metrics 写

所有写入走原子 .tmp → os.replace.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd

from .schema import (
    SELECTION_COLUMNS,
    ORDER_FIELD_TIMESTAMP,
)


class ResultStore:
    """主进程侧输出管理. Phase 2.4 落盘接口."""

    def __init__(self, output_root: str):
        self.output_root = Path(output_root)

    def day_dir(self, strategy_name: str, trade_date: date) -> Path:
        d = self.output_root / strategy_name / trade_date.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_selections(
        self,
        strategy_name: str,
        trade_date: date,
        selections: pd.DataFrame,
    ) -> Path:
        """写 selections.parquet (原子)."""
        target = self.day_dir(strategy_name, trade_date) / "selections.parquet"
        tmp = target.with_suffix(target.suffix + ".tmp")
        # reindex to canonical columns, missing → NaN
        df = selections.reindex(columns=SELECTION_COLUMNS)
        df.to_parquet(tmp, index=False)
        os.replace(tmp, target)
        return target

    def append_orders(
        self,
        strategy_name: str,
        trade_date: date,
        orders: Iterable[Dict[str, Any]],
    ) -> Path:
        """追加 orders.jsonl (每行一条 JSON).

        调用时机 = 下单后立即,每条 order 一条 line.
        """
        target = self.day_dir(strategy_name, trade_date) / "orders.jsonl"
        with open(target, "a", encoding="utf-8") as f:
            for order in orders:
                if ORDER_FIELD_TIMESTAMP not in order:
                    order[ORDER_FIELD_TIMESTAMP] = datetime.now().isoformat(timespec="seconds")
                f.write(json.dumps(order, ensure_ascii=False, default=str) + "\n")
        return target
