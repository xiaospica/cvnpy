"""[A1 Step 2a 闭环] 验证 template._persist_replay_equity_snapshot 真的把
回放权益写到 replay_history.db (而非旧的 mlearnweb.db).

这条测试是 A1/B2 解耦改动的最终防回归: 单元层面证明 vnpy 主进程在回放
settle 后调用本方法, 数据落到 vnpy 本地 SQLite, 由 mlearnweb 端 sync
service 接力同步.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# parents[2] = vnpy_strategy_dev repo root (本文件在 vnpy_ml_strategy/test/)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def isolated_replay_db(tmp_path, monkeypatch):
    """让 replay_history.db 写到 tmp_path (避免污染 D:/vnpy_data/state/)."""
    db_path = tmp_path / "replay_history.db"
    monkeypatch.setenv("REPLAY_HISTORY_DB", str(db_path))
    # 清模块级 init 缓存, 让新 db_path 走 DDL
    from vnpy_ml_strategy import replay_history
    replay_history._init_done.clear()
    return db_path


def _make_strategy_stub(strategy_name: str = "csi300_test"):
    """构造 MLStrategyTemplate 实例的最小桩, 仅含 _persist_replay_equity_snapshot 需要的字段."""
    from vnpy_ml_strategy.template import MLStrategyTemplate

    stub = MLStrategyTemplate.__new__(MLStrategyTemplate)
    stub.strategy_name = strategy_name
    stub.replay_status = "running"
    stub.replay_progress = "5/30"
    stub._replay_persist_logged_first = False
    # write_log 不写文件, 仅打印
    stub.write_log = lambda msg: print(f"[stub] {msg}")
    return stub


def _make_gateway_stub(cash: float, positions: list):
    """构造 gateway 桩: gateway.td.counter.{capital, frozen, positions}."""
    counter = MagicMock()
    counter.capital = cash + sum(p["volume"] * p["price"] + p["pnl"] for p in positions)
    counter.frozen = 0.0
    pos_dict = {}
    for i, p in enumerate(positions):
        pos = MagicMock()
        pos.volume = p["volume"]
        pos.price = p["price"]
        pos.pnl = p["pnl"]
        pos_dict[f"pos_{i}"] = pos
    counter.positions = pos_dict

    gateway = MagicMock()
    gateway.td.counter = counter
    return gateway


def test_persist_writes_to_replay_history_db(isolated_replay_db):
    """E2E 桩调用: 调 _persist_replay_equity_snapshot, 验证 replay_history.db 落了一行."""
    from vnpy_ml_strategy.replay_history import count_snapshots, list_snapshots

    stub = _make_strategy_stub("csi300_test")
    gateway = _make_gateway_stub(
        cash=500_000.0,
        positions=[
            {"volume": 1000, "price": 100.0, "pnl": 5_000.0},
            {"volume": 500, "price": 200.0, "pnl": -2_000.0},
        ],
    )

    stub._persist_replay_equity_snapshot(date(2026, 4, 30), gateway)

    # 验证 replay_history.db 有了一行
    assert count_snapshots("csi300_test", db_path=isolated_replay_db) == 1
    rows = list_snapshots("csi300_test", db_path=isolated_replay_db)
    r = rows[0]
    # equity = (500_000) + (1000×100 + 5_000) + (500×200 + (-2_000)) = 500_000 + 105_000 + 98_000 = 703_000
    # 注意 cash = capital - frozen, capital 桩中已经按 cash + 持仓市值算
    expected_market_value = 1000 * 100.0 + 5_000.0 + 500 * 200.0 + (-2_000.0)
    expected_equity = 500_000.0 + expected_market_value + expected_market_value  # capital 包含 market_value
    # 实际算: counter.capital - counter.frozen + sum(volume×price + pnl)
    # capital = 500_000 + 203_000 = 703_000; frozen = 0
    # cash = 703_000; market_value = 203_000; equity = 906_000
    assert r["strategy_value"] == pytest.approx(906_000.0)
    assert r["account_equity"] == pytest.approx(906_000.0)
    assert r["positions_count"] == 2
    # ts 是当日 15:00
    assert r["ts"] == "2026-04-30T15:00:00"
    # raw_variables 含 replay_status / replay_progress
    assert r["raw_variables"]["replay_status"] == "running"
    assert r["raw_variables"]["replay_progress"] == "5/30"


def test_persist_no_positions(isolated_replay_db):
    """空仓场景: 仅 cash, market_value=0, equity=cash."""
    from vnpy_ml_strategy.replay_history import list_snapshots

    stub = _make_strategy_stub("csi300_empty")
    gateway = _make_gateway_stub(cash=1_000_000.0, positions=[])

    stub._persist_replay_equity_snapshot(date(2026, 4, 28), gateway)

    rows = list_snapshots("csi300_empty", db_path=isolated_replay_db)
    assert len(rows) == 1
    assert rows[0]["strategy_value"] == pytest.approx(1_000_000.0)
    assert rows[0]["positions_count"] == 0


def test_persist_idempotent_same_day(isolated_replay_db):
    """同一 day 多次调 _persist (重跑回放) 应保持 1 行 (UPSERT)."""
    from vnpy_ml_strategy.replay_history import count_snapshots, list_snapshots

    stub = _make_strategy_stub("csi300_idempotent")
    g1 = _make_gateway_stub(cash=1_000_000.0, positions=[])
    g2 = _make_gateway_stub(cash=1_050_000.0, positions=[])

    stub._persist_replay_equity_snapshot(date(2026, 4, 30), g1)
    stub._persist_replay_equity_snapshot(date(2026, 4, 30), g2)

    assert count_snapshots("csi300_idempotent", db_path=isolated_replay_db) == 1
    rows = list_snapshots("csi300_idempotent", db_path=isolated_replay_db)
    assert rows[0]["strategy_value"] == pytest.approx(1_050_000.0)


def test_persist_does_not_raise_on_db_unavailable(monkeypatch, capfd):
    """db 路径写权限失败时, _persist 应只 log warn 不 raise (保护回放主循环)."""
    monkeypatch.setenv("REPLAY_HISTORY_DB", "/nonexistent/dir/that/cannot/be/created/replay.db")
    from vnpy_ml_strategy import replay_history
    replay_history._init_done.clear()

    stub = _make_strategy_stub("csi300_db_fail")
    gateway = _make_gateway_stub(cash=1_000_000.0, positions=[])

    # 不应 raise
    stub._persist_replay_equity_snapshot(date(2026, 4, 30), gateway)
