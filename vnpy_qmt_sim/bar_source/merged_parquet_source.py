# -*- coding: utf-8 -*-
"""按日读取 vnpy_tushare_pro 产出的 daily_merged_YYYYMMDD.parquet，原始名义价口径。

设计说明：柜台撮合用原始价格列（open/close/pre_close/up_limit/down_limit），
不用 _hfq 后复权列。原因：tushare 的 hfq_factor 基准会随 snapshot 末日动态归一，
同一日期在不同 snapshot 里 hfq 值不一致（采样验证：000001.SZ 2026-04-17 在
daily_merged_20260417.parquet 里 pre_close_hfq=59.66，在 daily_merged_20260422.parquet
里已重设为 11.09）。用原始价既保证跨 snapshot 一致性，也使 mlearnweb 持仓曲线
为真实名义资金值，与实盘 miniqmt 口径一致。qlib 模型只产选股 score，柜台记账口径独立。
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from vnpy.trader.utility import extract_vt_symbol

from ..utils import From_VN_Exchange_map
from .base import BarQuote, SimBarSource
from .registry import register_bar_source

logger = logging.getLogger(__name__)


def vt_symbol_to_ts_code(vt_symbol: str) -> str:
    """vnpy vt_symbol (000001.SZSE) → tushare ts_code (000001.SZ)."""
    symbol, exchange = extract_vt_symbol(vt_symbol)
    suffix = From_VN_Exchange_map.get(exchange)
    if not suffix:
        raise ValueError(f"cannot map vt_symbol {vt_symbol!r} to tushare ts_code")
    return f"{symbol}.{suffix}"


@register_bar_source("merged_parquet")
class MergedParquetBarSource(SimBarSource):
    name = "merged_parquet"

    _READ_COLS = [
        "ts_code", "trade_date", "name", "is_st", "suspend_timing", "delist_date",
        "open", "high", "low", "close", "pre_close",
        "up_limit", "down_limit", "pct_chg",
    ]

    def __init__(
        self,
        merged_root: str = r"D:\vnpy_data\snapshots\merged",
        reference_kind: str = "prev_close",
        fallback_days: int = 10,
        stale_warn_hours: int = 48,
        cache_max: int = 3,
    ) -> None:
        self.merged_root = Path(merged_root)
        self.reference_kind = reference_kind
        self.fallback_days = int(fallback_days)
        self.stale_warn_hours = float(stale_warn_hours)
        self._cache: "OrderedDict[str, Tuple[float, pd.DataFrame]]" = OrderedDict()
        self._cache_max = int(cache_max)
        self._stale_warned_for: set[str] = set()

    def _resolve_file(self, as_of_date: date) -> Optional[Path]:
        for offset in range(0, self.fallback_days):
            d = as_of_date - timedelta(days=offset)
            candidate = self.merged_root / f"daily_merged_{d:%Y%m%d}.parquet"
            if candidate.exists():
                return candidate
        return None

    def _load(self, path: Path) -> pd.DataFrame:
        key = path.name
        mtime = path.stat().st_mtime
        cached = self._cache.get(key)
        if cached is not None and cached[0] == mtime:
            self._cache.move_to_end(key)
            return cached[1]
        df = pd.read_parquet(path, columns=self._READ_COLS)
        df = df.set_index(["ts_code", "trade_date"]).sort_index()
        self._cache[key] = (mtime, df)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)
        self._check_freshness(path, mtime)
        return df

    def _check_freshness(self, path: Path, mtime: float) -> None:
        age_h = (time.time() - mtime) / 3600.0
        if age_h > self.stale_warn_hours and path.name not in self._stale_warned_for:
            logger.warning(
                "merged parquet %s 已 %.1fh 未更新，确认 TushareProApp 20:00 任务是否运行",
                path.name, age_h,
            )
            self._stale_warned_for.add(path.name)

    def get_quote(self, vt_symbol: str, as_of_date: date) -> Optional[BarQuote]:
        try:
            ts_code = vt_symbol_to_ts_code(vt_symbol)
        except ValueError:
            return None
        f = self._resolve_file(as_of_date)
        if f is None:
            return None
        df = self._load(f)
        try:
            rows = df.loc[ts_code]
        except KeyError:
            return None
        if isinstance(rows, pd.Series):
            rows = rows.to_frame().T
        mask = rows.index <= pd.Timestamp(as_of_date)
        if not mask.any():
            return None
        sub = rows.loc[mask]
        row = sub.iloc[-1]
        latest_date = sub.index[-1]
        if self.reference_kind == "today_open" and latest_date == pd.Timestamp(as_of_date):
            ref_price = float(row["open"])
        else:
            ref_price = float(row["pre_close"])
        pct_chg_raw = row.get("pct_chg") if hasattr(row, "get") else row["pct_chg"]
        pct_chg = float(pct_chg_raw) if pd.notna(pct_chg_raw) else 0.0
        return BarQuote(
            vt_symbol=vt_symbol,
            as_of_date=as_of_date,
            last_price=ref_price,
            pre_close=float(row["pre_close"]),
            open_price=float(row["open"]),
            high_price=float(row["high"]),
            low_price=float(row["low"]),
            limit_up=float(row["up_limit"]),
            limit_down=float(row["down_limit"]),
            pricetick=0.01,
            name=str(row["name"]) if pd.notna(row["name"]) else "",
            pct_chg=pct_chg,
        )

    def prefetch(self, vt_symbols, as_of_date) -> None:
        f = self._resolve_file(as_of_date)
        if f is not None:
            self._load(f)
