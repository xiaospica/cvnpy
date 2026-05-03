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
from datetime import date
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
        reference_kind: str = "today_open",
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
        """选包含 as_of_date 数据的 snapshot。

        重要：每个 daily_merged_YYYYMMDD.parquet 文件不是单日数据，
        而是含从约 4 个月前到文件名当日的**滚动窗口**全量历史
        (典型 78 个交易日 × 5500 只股票)。

        三优先级：
          1. 精确匹配 daily_merged_{as_of_date}.parquet — 实盘语义（每日 cron 写当日 snapshot）
          2. 未来兜底：文件名 ≥ as_of_date 的**最早**一个 — 回放语义
             （snapshot 含滚动窗口必含目标日；选最早保证数据"最贴近当时"）
          3. 过去兜底：文件名 < as_of_date 但 (as_of_date - file_date) ≤ fallback_days 的**最新**一个
             — 兼容周末查工作日 / cron 漏跑场景
          4. 都失败 → None

        优先级 2 是关键修复：用户机器 disk 上只有 4 月份 snapshot，但回放窗口从 1 月起；
        旧实现按"as_of_date - offset"向前找永远落空 → "bar_source 未命中"全屏刷。
        """
        if not self.merged_root.exists():
            logger.debug("merged_root %s 不存在", self.merged_root)
            return None

        # 1. 精确匹配（实盘 hot path，避免无谓扫目录）
        exact = self.merged_root / f"daily_merged_{as_of_date:%Y%m%d}.parquet"
        if exact.exists():
            logger.debug("[bar_source] %s exact-match → %s", as_of_date, exact.name)
            return exact

        # 扫目录，构造 (file_date, path) 列表
        snapshots: list[Tuple[date, Path]] = []
        for entry in self.merged_root.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if not name.startswith("daily_merged_") or not name.endswith(".parquet"):
                continue
            stem = entry.stem.replace("daily_merged_", "")
            if len(stem) != 8 or not stem.isdigit():
                continue
            try:
                d = date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
            except ValueError:
                continue
            snapshots.append((d, entry))

        if not snapshots:
            logger.warning("[bar_source] %s 目录无任何 daily_merged_*.parquet", self.merged_root)
            return None

        snapshots.sort()

        # 2. 未来兜底
        future_candidates = [(d, p) for d, p in snapshots if d >= as_of_date]
        if future_candidates:
            picked_d, picked_p = future_candidates[0]
            logger.info(
                "[bar_source] %s 无精确 snapshot，回放兜底用未来 snapshot %s (距目标日 +%dd)",
                as_of_date, picked_p.name, (picked_d - as_of_date).days,
            )
            return picked_p

        # 3. 过去兜底（fallback_days 内）
        latest_date, latest_path = snapshots[-1]
        gap = (as_of_date - latest_date).days
        if gap <= self.fallback_days:
            logger.info(
                "[bar_source] %s 无未来 snapshot，过去兜底用 %s (距目标日 -%dd ≤ fallback_days=%d)",
                as_of_date, latest_path.name, gap, self.fallback_days,
            )
            return latest_path

        # 4. 都失败
        logger.warning(
            "[bar_source] %s 解析失败：最新 snapshot %s 距目标日 -%dd 超出 fallback_days=%d，未来 snapshot 无",
            as_of_date, latest_path.name, gap, self.fallback_days,
        )
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
            close_price=float(row["close"]),
        )

    def prefetch(self, vt_symbols, as_of_date) -> None:
        f = self._resolve_file(as_of_date)
        if f is not None:
            self._load(f)
