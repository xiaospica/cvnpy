"""[A1 E2E 验证用] 极简 fake webtrader uvicorn 服务,模拟 vnpy 节点对外 HTTP API.

绕过 vnpy MainEngine + RPC server, 直接 import replay_history.list_snapshots.
仅暴露 mlearnweb 当前所有 ml_snapshot_loop / replay_equity_sync_loop / health
discovery 用到的端点, 让 mlearnweb 端能完整跑通 fanout sync 链路验证.

⚠️ 仅用于 A1 E2E 冒烟. 不是生产代码 — vnpy_webtrader 真实实现见
[vnpy_webtrader/web.py](../vnpy_webtrader/web.py).

启动:
    F:/Program_Home/vnpy/python.exe tests/fake_webtrader_for_a1_e2e.py

mlearnweb 端 vnpy_nodes.yaml 配 base_url=http://127.0.0.1:8001 即可拉到数据.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
import uvicorn

# parents[2] = vnpy_strategy_dev repo root (本文件在 vnpy_ml_strategy/test/)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from vnpy_ml_strategy.replay_history import list_snapshots


app = FastAPI(title="fake_webtrader (A1 E2E 验证)")


# ---- token (mlearnweb _PerNodeClient 会先 POST /api/v1/token 拿 JWT) ----

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


@app.post("/api/v1/token", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    # 简化: 不校验 username/password, 任何凭证都返回固定 token
    return Token(access_token="fake-jwt-token-for-a1-e2e-verification")


# ---- ML health (discovery 接口, mlearnweb sync 用 get_ml_health_all) ----

@app.get("/api/v1/ml/health")
async def ml_health() -> Dict[str, Any]:
    """返回 (注入到 replay_history.db 的) 策略列表.

    真实 vnpy_webtrader.routes_ml 用 unwrap_result() 解包 envelope, 直接返
    内层 data; 这里 fake 也不包 envelope, 与真实行为一致.
    """
    return {
        "strategies": [
            {
                "name": "csi300_lgb_headless",
                "last_run_date": "",
                "last_status": "ok",
                "last_error": "",
                "last_model_run_id": "",
                "last_n_pred": 0,
                "last_duration_ms": 0,
            },
            {
                "name": "csi300_lgb_headless_2",
                "last_run_date": "",
                "last_status": "ok",
                "last_error": "",
                "last_model_run_id": "",
                "last_n_pred": 0,
                "last_duration_ms": 0,
            },
        ]
    }


# ---- A1 新增的回放权益快照 ----

@app.get("/api/v1/ml/strategies/{name}/replay/equity_snapshots")
async def ml_replay_equity_snapshots(
    name: str,
    since: Optional[str] = Query(None),
    limit: int = Query(10000, ge=1, le=100000),
) -> List[Dict[str, Any]]:
    """直接调 vnpy 端的 replay_history.list_snapshots."""
    return list_snapshots(name, since_iso=since, limit=limit)


# ---- 兜底: mlearnweb 其他 loop 调到的端点不要 500, 返空就行 (无 envelope) ----

@app.get("/api/v1/ml/strategies/{name}/metrics/latest")
async def ml_metrics_latest(name: str) -> Dict[str, Any]:
    return {}


@app.get("/api/v1/ml/strategies/{name}/metrics")
async def ml_metrics_history(name: str, days: int = 30) -> List[Dict[str, Any]]:
    return []


@app.get("/api/v1/ml/strategies/{name}/prediction/latest/summary")
async def ml_prediction_summary(name: str) -> Dict[str, Any]:
    return {}


@app.get("/api/v1/strategy")
async def strategies() -> List[Dict[str, Any]]:
    """live_trading_service 的 strategies_fanout 用; 返空避免 snapshot_loop 抛错."""
    return []


@app.get("/api/v1/account")
async def accounts() -> List[Dict[str, Any]]:
    return []


@app.get("/api/v1/position")
async def positions() -> List[Dict[str, Any]]:
    return []


@app.get("/api/v1/node/info")
async def node_info() -> Dict[str, Any]:
    return {"node_id": "local", "display_name": "fake-webtrader-a1-e2e"}


if __name__ == "__main__":
    print("[fake_webtrader] 启动在 http://127.0.0.1:8001")
    print("[fake_webtrader] /api/v1/ml/strategies/<name>/replay/equity_snapshots 直读 D:/vnpy_data/state/replay_history.db")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
