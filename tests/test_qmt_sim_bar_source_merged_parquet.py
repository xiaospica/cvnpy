from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from vnpy_qmt_sim.bar_source.merged_parquet_source import (
    MergedParquetBarSource,
    vt_symbol_to_ts_code,
)


def _make_snapshot(tmp_path: Path, snapshot_date: str, rows: list[dict]) -> Path:
    """Build a minimal daily_merged_YYYYMMDD.parquet with the columns the source reads."""
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    if "delist_date" not in df.columns:
        df["delist_date"] = pd.NaT
    if "list_date" not in df.columns:
        df["list_date"] = pd.NaT
    out = tmp_path / f"daily_merged_{snapshot_date}.parquet"
    df.to_parquet(out)
    return out


def _row(ts_code: str, trade_date: str, close: float, pre_close: float | None = None, **over) -> dict:
    pc = pre_close if pre_close is not None else close
    pct = (close / pc - 1.0) * 100.0 if pc else 0.0
    r = {
        "ts_code": ts_code,
        "trade_date": trade_date,
        "name": "TEST",
        "is_st": "N",
        "suspend_timing": None,
        "delist_date": None,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": pc,
        "up_limit": round(pc * 1.1, 2),
        "down_limit": round(pc * 0.9, 2),
        "pct_chg": pct,
    }
    r.update(over)
    return r


def test_vt_symbol_to_ts_code() -> None:
    assert vt_symbol_to_ts_code("000001.SZSE") == "000001.SZ"
    assert vt_symbol_to_ts_code("600000.SSE") == "600000.SH"


def test_get_quote_prev_close_mode(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-21", close=11.06),
        _row("000001.SZ", "2026-04-22", close=10.98, pre_close=11.08),
    ])
    src = MergedParquetBarSource(merged_root=str(tmp_path), reference_kind="prev_close")
    q = src.get_quote("000001.SZSE", date(2026, 4, 22))
    assert q is not None
    assert q.last_price == pytest.approx(11.08)
    assert q.pre_close == pytest.approx(11.08)
    assert q.limit_up == pytest.approx(12.19)
    assert q.limit_down == pytest.approx(9.97)


def test_get_quote_today_open_mode(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-22", close=10.98, pre_close=11.08, open=11.10),
    ])
    src = MergedParquetBarSource(merged_root=str(tmp_path), reference_kind="today_open")
    q = src.get_quote("000001.SZSE", date(2026, 4, 22))
    assert q.last_price == pytest.approx(11.10)


def test_weekend_fallback(tmp_path: Path) -> None:
    # Friday snapshot present, Saturday absent; Saturday query should fall back to Friday.
    _make_snapshot(tmp_path, "20260417", [
        _row("000001.SZ", "2026-04-17", close=11.01, pre_close=11.09),
    ])
    src = MergedParquetBarSource(merged_root=str(tmp_path))
    q = src.get_quote("000001.SZSE", date(2026, 4, 18))  # Saturday
    assert q is not None
    assert q.pre_close == pytest.approx(11.09)


def test_missing_symbol_returns_none(tmp_path: Path) -> None:
    _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-22", close=10.98),
    ])
    src = MergedParquetBarSource(merged_root=str(tmp_path))
    assert src.get_quote("999999.SZSE", date(2026, 4, 22)) is None


def test_no_snapshot_in_fallback_window_returns_none(tmp_path: Path) -> None:
    src = MergedParquetBarSource(merged_root=str(tmp_path), fallback_days=3)
    assert src.get_quote("000001.SZSE", date(2026, 4, 22)) is None


def test_mtime_cache_invalidation(tmp_path: Path) -> None:
    p = _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-22", close=10.0, pre_close=10.0),
    ])
    src = MergedParquetBarSource(merged_root=str(tmp_path))
    q1 = src.get_quote("000001.SZSE", date(2026, 4, 22))
    assert q1.pre_close == pytest.approx(10.0)

    # Rewrite snapshot with different price and bump mtime.
    p.unlink()
    _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-22", close=20.0, pre_close=20.0),
    ])
    os.utime(p, (time.time() + 10, time.time() + 10))

    q2 = src.get_quote("000001.SZSE", date(2026, 4, 22))
    assert q2.pre_close == pytest.approx(20.0), "source should invalidate cache when file mtime changes"


def test_stale_warning_emitted_once(tmp_path: Path, caplog) -> None:
    p = _make_snapshot(tmp_path, "20260422", [
        _row("000001.SZ", "2026-04-22", close=10.0),
    ])
    old = time.time() - 72 * 3600
    os.utime(p, (old, old))

    src = MergedParquetBarSource(merged_root=str(tmp_path), stale_warn_hours=48)
    with caplog.at_level("WARNING"):
        src.get_quote("000001.SZSE", date(2026, 4, 22))
        src.get_quote("000001.SZSE", date(2026, 4, 22))

    warn_lines = [r for r in caplog.records if "未更新" in r.getMessage()]
    assert len(warn_lines) == 1, f"stale warning should dedup per file, got {len(warn_lines)}"
