"""Phase 2.4 — ResultStore 原子写 + selection/order schema."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from vnpy_ml_strategy.persistence.result_store import ResultStore
from vnpy_ml_strategy.persistence.schema import (
    COL_INSTRUMENT,
    COL_MODEL_RUN_ID,
    COL_RANK,
    COL_SCORE,
    COL_SIDE,
    COL_TRADE_DATE,
    COL_WEIGHT,
    COL_TARGET_PRICE,
    SELECTION_COLUMNS,
)


def test_write_selections_roundtrip(tmp_path):
    store = ResultStore(str(tmp_path))
    today = date(2026, 4, 20)
    df = pd.DataFrame({
        COL_TRADE_DATE: ["2026-04-20"] * 3,
        COL_INSTRUMENT: ["000001.SZ", "000002.SZ", "600000.SH"],
        COL_SCORE: [0.3, 0.2, 0.1],
        COL_RANK: [1, 2, 3],
        COL_WEIGHT: [0.33, 0.33, 0.34],
        COL_TARGET_PRICE: [float("nan")] * 3,
        COL_SIDE: ["long"] * 3,
        COL_MODEL_RUN_ID: ["abc"] * 3,
    })
    path = store.write_selections("demo", today, df)

    assert path.exists()
    loaded = pd.read_parquet(path)
    assert list(loaded.columns) == SELECTION_COLUMNS
    assert loaded.shape == (3, len(SELECTION_COLUMNS))


def test_append_orders_jsonl(tmp_path):
    store = ResultStore(str(tmp_path))
    today = date(2026, 4, 20)
    orders = [
        {"instrument": "000001.SZ", "direction": "long", "status": "submitted", "price": 10.0, "volume": 100},
        {"instrument": "000002.SZ", "direction": "long", "status": "filtered_out",
         "filter_reason": "limit_up", "price": 0, "volume": 100},
    ]
    path = store.append_orders("demo", today, orders)

    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["instrument"] == "000001.SZ"
    assert parsed[1]["filter_reason"] == "limit_up"
    # every order got a timestamp injected
    assert all("timestamp" in p for p in parsed)


def test_append_orders_appends_not_overwrites(tmp_path):
    store = ResultStore(str(tmp_path))
    today = date(2026, 4, 20)
    store.append_orders("demo", today, [{"instrument": "A", "status": "s"}])
    store.append_orders("demo", today, [{"instrument": "B", "status": "s"}])
    path = store.day_dir("demo", today) / "orders.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
