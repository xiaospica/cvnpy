"""Phase 2.5 — MetricsCache + publish_metrics + latest.json 原子写"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from vnpy_ml_strategy import MLStrategyApp, APP_NAME
from vnpy_ml_strategy.monitoring.cache import MetricsCache
from vnpy_ml_strategy.monitoring.publisher import publish_metrics


def test_metrics_cache_basic():
    cache = MetricsCache(max_history_days=5)
    cache.update("s1", {"ic": 0.1})
    cache.update("s1", {"ic": 0.2})
    cache.update("s2", {"ic": 0.3})

    assert cache.get_latest("s1") == {"ic": 0.2}
    assert len(cache.get_history("s1")) == 2
    assert cache.get_latest("s2") == {"ic": 0.3}
    assert "s1" in cache.list_strategies()
    assert "s2" in cache.list_strategies()


def test_publish_metrics_writes_latest_json_on_ok(tmp_path):
    cache = MetricsCache()
    strat = "unit_test_strat"
    today = date.today()
    day_dir = tmp_path / strat / today.strftime("%Y%m%d")
    day_dir.mkdir(parents=True)
    src = day_dir / "metrics.json"
    src.write_text(json.dumps({"ic": 0.05, "psi_mean": 0.1}))

    latest = publish_metrics(
        cache, strat, today, str(tmp_path),
        metrics={"ic": 0.05, "psi_mean": 0.1},
        status="ok",
    )

    assert latest.exists()
    data = json.loads(latest.read_text())
    assert data["ic"] == 0.05
    assert cache.get_latest(strat) == {"ic": 0.05, "psi_mean": 0.1}


def test_publish_metrics_failed_does_not_overwrite_latest(tmp_path):
    """failed 状态不该覆盖上一次成功的 latest.json — 保留上次监控数据."""
    cache = MetricsCache()
    strat = "flaky_strat"
    today = date.today()
    day_dir = tmp_path / strat / today.strftime("%Y%m%d")
    day_dir.mkdir(parents=True)
    (day_dir / "metrics.json").write_text(json.dumps({"ic": 0.3}))

    # 1. 先成功一次
    publish_metrics(cache, strat, today, str(tmp_path),
                    metrics={"ic": 0.3}, status="ok")
    latest = tmp_path / strat / "latest.json"
    assert json.loads(latest.read_text())["ic"] == 0.3

    # 2. 失败一次,latest.json 内容不应被覆盖
    publish_metrics(cache, strat, today, str(tmp_path),
                    metrics={}, status="failed")
    assert json.loads(latest.read_text())["ic"] == 0.3


def test_engine_publish_metrics_emits_event(tmp_path):
    # MainEngine constructor auto-starts the EventEngine; no explicit start() needed.
    ev = EventEngine()
    main = MainEngine(ev)
    main.add_app(MLStrategyApp)
    eng = main.get_engine(APP_NAME)

    received = []
    ev.register("eMlMetrics.e_test", lambda e: received.append(e.type))

    strat = "e_test"
    today = date.today()
    day_dir = tmp_path / strat / today.strftime("%Y%m%d")
    day_dir.mkdir(parents=True)
    (day_dir / "metrics.json").write_text(json.dumps({"ic": 0.1}))

    eng.publish_metrics(strat, {"ic": 0.1}, trade_date=today,
                        output_root=str(tmp_path), status="ok")

    import time; time.sleep(0.5)

    assert "eMlMetrics.e_test" in received
    assert eng.get_latest_metrics(strat) == {"ic": 0.1}
    main.close()
