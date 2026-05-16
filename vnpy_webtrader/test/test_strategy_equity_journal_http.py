"""HTTP tests for the strategy equity journal route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeFastRpcClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def get_strategy_equity_journal(
        self,
        engine: str,
        strategy_name: str,
        since: str | None,
        source_label: str | None,
        limit: int,
    ) -> dict[str, Any]:
        self.calls.append({
            "engine": engine,
            "strategy_name": strategy_name,
            "since": since,
            "source_label": source_label,
            "limit": limit,
        })
        return self.result


def _make_client(monkeypatch, fake_rpc: FakeFastRpcClient) -> TestClient:
    from vnpy_webtrader import routes_strategy
    from vnpy_webtrader.deps import get_access
    from vnpy_webtrader.routes_strategy import router

    monkeypatch.setattr(routes_strategy, "get_fast_rpc_client", lambda: fake_rpc)

    app = FastAPI()
    app.dependency_overrides[get_access] = lambda: True
    app.include_router(router)
    return TestClient(app)


def test_strategy_equity_journal_route_passes_query_to_rpc(monkeypatch) -> None:
    fake_rpc = FakeFastRpcClient({
        "ok": True,
        "message": "",
        "data": [
            {
                "seq": 1,
                "engine": "SignalStrategyPlus",
                "strategy_name": "alpha",
                "source_label": "broker_live_close",
                "ts": "2026-05-15T15:00:00",
                "strategy_value": 1000200.0,
            }
        ],
    })
    client = _make_client(monkeypatch, fake_rpc)

    resp = client.get(
        "/api/v1/strategy/equity-journal",
        params={
            "engine": "SignalStrategyPlus",
            "strategy_name": "alpha",
            "since": "2026-05-14T15:00:00",
            "source_label": "broker_live_close",
            "limit": 123,
        },
    )

    assert resp.status_code == 200
    assert resp.json()[0]["strategy_value"] == 1000200.0
    assert fake_rpc.calls == [
        {
            "engine": "SignalStrategyPlus",
            "strategy_name": "alpha",
            "since": "2026-05-14T15:00:00",
            "source_label": "broker_live_close",
            "limit": 123,
        }
    ]


def test_strategy_equity_journal_route_unwraps_rpc_error(monkeypatch) -> None:
    fake_rpc = FakeFastRpcClient({
        "ok": False,
        "message": "strategy journal unavailable",
        "data": {"http_status": 404},
    })
    client = _make_client(monkeypatch, fake_rpc)

    resp = client.get(
        "/api/v1/strategy/equity-journal",
        params={
            "engine": "SignalStrategyPlus",
            "strategy_name": "missing",
        },
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "strategy journal unavailable"
