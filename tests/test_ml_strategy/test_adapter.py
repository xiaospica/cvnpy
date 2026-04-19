"""Phase 2.6 — webtrader MLStrategyAdapter 契约测试."""

from __future__ import annotations

from unittest.mock import MagicMock

from vnpy_webtrader.strategy_adapter import (
    ADAPTER_REGISTRY,
    MLStrategyAdapter,
)


def test_adapter_registered():
    assert "MlStrategy" in ADAPTER_REGISTRY
    assert ADAPTER_REGISTRY["MlStrategy"] is MLStrategyAdapter


def test_describe_contract():
    eng = MagicMock()
    adapter = MLStrategyAdapter(eng)
    d = adapter.describe()
    assert d["app_name"] == "MlStrategy"
    assert d["event_type"] == "eMlStrategy"
    assert set(["add", "init", "start", "stop", "remove"]).issubset(d["capabilities"])


def test_get_latest_metrics_delegates_to_engine():
    eng = MagicMock()
    eng.get_latest_metrics.return_value = {"ic": 0.1}
    adapter = MLStrategyAdapter(eng)
    assert adapter.get_latest_metrics("demo") == {"ic": 0.1}
    eng.get_latest_metrics.assert_called_once_with("demo")


def test_get_latest_metrics_returns_none_when_engine_missing_method():
    # If engine doesn't expose the method, adapter returns None (defensive)
    class Bare:
        pass
    adapter = MLStrategyAdapter(Bare())
    assert adapter.get_latest_metrics("demo") is None


def test_get_prediction_summary_assembles_from_latest_metrics():
    eng = MagicMock()
    eng.get_latest_metrics.return_value = {
        "n_predictions": 100,
        "score_histogram": [{"bin_id": 0}],
        "pred_mean": 0.01,
        "pred_std": 0.05,
        "trade_date": "2026-04-20",
        "model_run_id": "abc",
    }
    adapter = MLStrategyAdapter(eng)
    summary = adapter.get_prediction_summary("demo")
    assert summary["n_symbols"] == 100
    assert summary["pred_mean"] == 0.01
    assert len(summary["score_histogram"]) == 1
    assert summary["model_run_id"] == "abc"


def test_get_health_iterates_strategies():
    eng = MagicMock()
    eng.strategies = {
        "s1": MagicMock(last_run_date="2026-04-19", last_status="ok",
                        last_error="", last_model_run_id="abc",
                        last_n_pred=100, last_duration_ms=95000),
    }
    adapter = MLStrategyAdapter(eng)
    health = adapter.get_health()
    assert len(health["strategies"]) == 1
    assert health["strategies"][0]["name"] == "s1"
    assert health["strategies"][0]["last_status"] == "ok"
