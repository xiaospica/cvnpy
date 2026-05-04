"""replay_history.py 单元测试.

验证:
- write_snapshot UPSERT 语义 (同 (strategy, ts) 重写最新)
- list_snapshots 按 strategy_name 过滤 + since_iso 增量
- count_snapshots
- 路径解析 (REPLAY_HISTORY_DB env > QS_DATA_ROOT > 默认)

Run:
    F:/Program_Home/vnpy/python.exe -m pytest tests/test_replay_history.py -v
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vnpy_ml_strategy.replay_history import (
    _resolve_db_path,
    count_snapshots,
    list_snapshots,
    write_snapshot,
)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    """每测试用例一个独立 db, 避免互相干扰."""
    return tmp_path / "test_replay.db"


def test_write_and_list_basic(tmp_db):
    ts = datetime(2026, 1, 5, 15, 0, 0)
    ok = write_snapshot(
        strategy_name="csi300_a",
        ts=ts,
        strategy_value=1_000_000.0,
        account_equity=1_000_000.0,
        positions_count=7,
        db_path=tmp_db,
    )
    assert ok is True
    assert tmp_db.exists()

    rows = list_snapshots("csi300_a", db_path=tmp_db)
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_name"] == "csi300_a"
    assert r["ts"] == ts.isoformat()
    assert r["strategy_value"] == 1_000_000.0
    assert r["positions_count"] == 7


def test_upsert_same_strategy_ts(tmp_db):
    """重复写同 (strategy, ts) 应只保留最新值."""
    ts = datetime(2026, 1, 5, 15, 0, 0)
    write_snapshot(
        strategy_name="csi300_a", ts=ts, strategy_value=1_000_000.0,
        account_equity=1_000_000.0, db_path=tmp_db,
    )
    write_snapshot(
        strategy_name="csi300_a", ts=ts, strategy_value=1_050_000.0,
        account_equity=1_050_000.0, positions_count=5, db_path=tmp_db,
    )
    rows = list_snapshots("csi300_a", db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["strategy_value"] == 1_050_000.0
    assert rows[0]["positions_count"] == 5


def test_list_filters_by_strategy_name(tmp_db):
    """同 db 多策略写, list_snapshots 按 strategy_name 精确过滤."""
    ts = datetime(2026, 1, 5, 15, 0, 0)
    write_snapshot(strategy_name="csi300_a", ts=ts, strategy_value=1.0,
                   account_equity=1.0, db_path=tmp_db)
    write_snapshot(strategy_name="csi300_b", ts=ts, strategy_value=2.0,
                   account_equity=2.0, db_path=tmp_db)

    rows_a = list_snapshots("csi300_a", db_path=tmp_db)
    rows_b = list_snapshots("csi300_b", db_path=tmp_db)
    assert len(rows_a) == 1 and rows_a[0]["strategy_value"] == 1.0
    assert len(rows_b) == 1 and rows_b[0]["strategy_value"] == 2.0


def test_list_orders_by_ts_asc(tmp_db):
    """list_snapshots 按 ts ASC 排序."""
    base = datetime(2026, 1, 5, 15, 0, 0)
    for i in (3, 1, 2):
        write_snapshot(
            strategy_name="csi300_a",
            ts=base + timedelta(days=i),
            strategy_value=1_000_000.0 + i * 1000,
            account_equity=1_000_000.0 + i * 1000,
            db_path=tmp_db,
        )
    rows = list_snapshots("csi300_a", db_path=tmp_db)
    assert [r["strategy_value"] for r in rows] == [
        1_001_000.0, 1_002_000.0, 1_003_000.0,
    ]


def test_list_since_iso_increment(tmp_db):
    """since_iso 增量拉取语义 (mlearnweb sync service 模拟)."""
    base = datetime(2026, 1, 5, 15, 0, 0)
    write_snapshot(strategy_name="csi300_a", ts=base, strategy_value=1.0,
                   account_equity=1.0, db_path=tmp_db)
    # 拿到第一行的 inserted_at, 用作 since 增量边界
    first_rows = list_snapshots("csi300_a", db_path=tmp_db)
    assert len(first_rows) == 1
    first_inserted_at = first_rows[0]["inserted_at"]

    # 模拟时间过去, 再写一行
    time.sleep(1.1)  # 确保 inserted_at 进位 (SQLite CURRENT_TIMESTAMP 秒级)
    write_snapshot(
        strategy_name="csi300_a", ts=base + timedelta(days=1),
        strategy_value=2.0, account_equity=2.0, db_path=tmp_db,
    )

    # since 取第一行 inserted_at + 1s, 只应返回第二行
    cutoff = (datetime.fromisoformat(first_inserted_at) + timedelta(seconds=1)).isoformat()
    rows = list_snapshots("csi300_a", since_iso=cutoff, db_path=tmp_db)
    assert len(rows) == 1
    assert rows[0]["strategy_value"] == 2.0


def test_list_empty_when_db_not_exist(tmp_path):
    """db 文件不存在时返空列表 (而非 raise)."""
    rows = list_snapshots("any", db_path=tmp_path / "nonexistent.db")
    assert rows == []


def test_count_snapshots(tmp_db):
    base = datetime(2026, 1, 5, 15, 0, 0)
    for i in range(3):
        write_snapshot(
            strategy_name="csi300_a", ts=base + timedelta(days=i),
            strategy_value=1.0, account_equity=1.0, db_path=tmp_db,
        )
    write_snapshot(
        strategy_name="csi300_b", ts=base, strategy_value=1.0,
        account_equity=1.0, db_path=tmp_db,
    )
    assert count_snapshots("csi300_a", db_path=tmp_db) == 3
    assert count_snapshots("csi300_b", db_path=tmp_db) == 1
    assert count_snapshots(db_path=tmp_db) == 4


def test_resolve_db_path_explicit_env(monkeypatch, tmp_path):
    explicit = tmp_path / "custom.db"
    monkeypatch.setenv("REPLAY_HISTORY_DB", str(explicit))
    assert _resolve_db_path() == explicit


def test_resolve_db_path_qs_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("REPLAY_HISTORY_DB", raising=False)
    monkeypatch.setenv("QS_DATA_ROOT", str(tmp_path))
    expected = tmp_path / "state" / "replay_history.db"
    assert _resolve_db_path() == expected


def test_resolve_db_path_default(monkeypatch):
    monkeypatch.delenv("REPLAY_HISTORY_DB", raising=False)
    monkeypatch.delenv("QS_DATA_ROOT", raising=False)
    p = _resolve_db_path()
    assert "vnpy_data" in str(p) or "state" in str(p)
    assert str(p).endswith("replay_history.db")


def test_raw_variables_roundtrip(tmp_db):
    ts = datetime(2026, 1, 5, 15, 0, 0)
    raw = {"replay_status": "running", "replay_progress": "5/30", "topk": 7}
    write_snapshot(
        strategy_name="csi300_a", ts=ts, strategy_value=1.0,
        account_equity=1.0, raw_variables=raw, db_path=tmp_db,
    )
    rows = list_snapshots("csi300_a", db_path=tmp_db)
    assert rows[0]["raw_variables"] == raw
