"""企业行为 (corp_action) 检测 — vnpy 端实现, Phase 3.3 HTTP 化.

之前 mlearnweb ``corp_actions_service.py`` 直读
``{QS_DATA_ROOT}/snapshots/merged/daily_merged_{T}.parquet`` 跑算法, 跨机
部署时 mlearnweb 拿不到这个本地文件. 现在算法搬到 vnpy 端 (数据原生位置)
+ 包成 HTTP 端点 (``/api/v1/reference/corp_actions``), mlearnweb 退化成
HTTP 客户端.

检测逻辑保持原 mlearnweb 实现一致:
    pct_chg (tushare 复权涨跌幅)  vs  raw_change = close[T]/close[T-1]-1
    差异 > threshold_pct → 当日除权事件

复用 ``DailyIngestPipeline`` 同一份 ``snapshots/merged`` 目录约定, 路径优先级:
    1. env ``ML_SNAPSHOT_DIR`` (绝对路径)
    2. ``${QS_DATA_ROOT}/snapshots`` (默认 D:/vnpy_data/snapshots)

不依赖 EventEngine / RpcServer — 纯本地文件 + pandas 算法, 给 HTTP layer 直接调.
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorpActionEvent:
    vt_symbol: str
    name: str
    trade_date: str
    pct_chg: float
    raw_change_pct: float
    magnitude_pct: float
    pre_close: float
    close: float


def _vt_to_ts(vt: str) -> Optional[str]:
    if "." not in vt:
        return None
    sym, ex = vt.rsplit(".", 1)
    suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}.get(ex.upper())
    if suffix is None:
        return None
    return f"{sym}.{suffix}"


_FILE_CACHE: "OrderedDict[str, tuple[float, pd.DataFrame]]" = OrderedDict()
_CACHE_MAX = 3
_READ_COLS = ["ts_code", "trade_date", "name", "close", "pre_close", "pct_chg"]


def _resolve_snapshot_dir() -> Path:
    """``ML_SNAPSHOT_DIR`` 优先, 否则 ``${QS_DATA_ROOT}/snapshots``,
    最终回退 ``D:/vnpy_data/snapshots``. 与 ``DailyIngestPipeline`` 默认一致.
    """
    explicit = os.getenv("ML_SNAPSHOT_DIR")
    if explicit:
        return Path(explicit)
    qs_root = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
    return Path(qs_root) / "snapshots"


def _resolve_merged_snapshot(as_of: date, fallback_days: int = 10) -> Optional[Path]:
    merged_dir = _resolve_snapshot_dir() / "merged"
    if not merged_dir.exists():
        return None
    for offset in range(0, fallback_days):
        candidate = merged_dir / f"daily_merged_{(as_of - timedelta(days=offset)):%Y%m%d}.parquet"
        if candidate.exists():
            return candidate
    return None


def _load_snapshot(path: Path) -> pd.DataFrame:
    key = path.name
    mtime = path.stat().st_mtime
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        _FILE_CACHE.move_to_end(key)
        return cached[1]
    df = pd.read_parquet(path, columns=_READ_COLS)
    df = df.set_index(["ts_code", "trade_date"]).sort_index()
    _FILE_CACHE[key] = (mtime, df)
    while len(_FILE_CACHE) > _CACHE_MAX:
        _FILE_CACHE.popitem(last=False)
    return df


def detect_corp_actions(
    vt_symbols: Iterable[str],
    *,
    as_of: Optional[date] = None,
    lookback_days: int = 30,
    threshold_pct: float = 0.5,
) -> List[CorpActionEvent]:
    """检测最近 lookback_days 内的除权事件.

    与 mlearnweb 老 ``corp_actions_service.detect_corp_actions`` 算法等价 —
    pct_chg vs raw_change 绝对差 > threshold 即判定除权.
    """
    end = as_of or datetime.now().date()
    snapshot = _resolve_merged_snapshot(end)
    if snapshot is None:
        logger.info(
            "[corp_actions] 未找到 %s 之前 10 日内的 daily_merged 文件 (snapshot_dir=%s)",
            end, _resolve_snapshot_dir(),
        )
        return []

    df = _load_snapshot(snapshot)
    start = end - timedelta(days=lookback_days)
    events: List[CorpActionEvent] = []
    for vt in set(vt_symbols):
        ts_code = _vt_to_ts(vt)
        if ts_code is None:
            continue
        try:
            rows = df.loc[ts_code]
        except KeyError:
            continue
        if isinstance(rows, pd.Series):
            rows = rows.to_frame().T

        mask = (rows.index >= pd.Timestamp(start)) & (rows.index <= pd.Timestamp(end))
        sub = rows.loc[mask].sort_index()
        if len(sub) < 2:
            continue

        closes = sub["close"].values
        for i in range(1, len(sub)):
            prev_close = float(closes[i - 1])
            today_close = float(closes[i])
            today_pre_close = float(sub["pre_close"].iloc[i])
            pct_chg = float(sub["pct_chg"].iloc[i])
            if prev_close <= 0 or pd.isna(today_pre_close):
                continue

            raw_change_pct = (today_close / prev_close - 1.0) * 100.0
            magnitude = abs(pct_chg - raw_change_pct)
            if magnitude < threshold_pct:
                continue

            ts_dt: pd.Timestamp = sub.index[i]  # type: ignore[assignment]
            events.append(CorpActionEvent(
                vt_symbol=vt,
                name=str(sub["name"].iloc[i]) if pd.notna(sub["name"].iloc[i]) else "",
                trade_date=ts_dt.date().isoformat(),
                pct_chg=round(pct_chg, 4),
                raw_change_pct=round(raw_change_pct, 4),
                magnitude_pct=round(magnitude, 4),
                pre_close=round(today_pre_close, 4),
                close=round(today_close, 4),
            ))

    events.sort(key=lambda e: (e.trade_date, e.vt_symbol), reverse=True)
    return events


def serialize_events(events: List[CorpActionEvent]) -> List[dict]:
    """dataclass → dict, HTTP layer 序列化用."""
    return [asdict(e) for e in events]
