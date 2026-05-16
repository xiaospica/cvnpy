"""[A1 E2E 验证用] 极简 fake webtrader uvicorn 服务,模拟 vnpy 节点对外 HTTP API.

绕过 vnpy MainEngine + RPC server, 直接读取 common strategy equity journal.
仅暴露 mlearnweb 当前所有 ml_snapshot_loop / strategy_equity_journal_sync_loop / health
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
from vnpy_common.persistence.strategy_equity_journal import list_snapshots


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
    """返回 (注入到 strategy_equity_journal.db 的) 策略列表.

    真实 vnpy_webtrader.routes_ml 用 unwrap_result() 解包 envelope, 直接返
    内层 data; 这里 fake 也不包 envelope, 与真实行为一致.
    """
    return {
        "strategies": [
            {
                "name": "csi300_lgb_headless",
                "engine": "MlStrategy",
                "last_run_date": "",
                "last_status": "ok",
                "last_error": "",
                "last_model_run_id": "",
                "last_n_pred": 0,
                "last_duration_ms": 0,
            },
            {
                "name": "csi300_lgb_headless_2",
                "engine": "MlStrategy",
                "last_run_date": "",
                "last_status": "ok",
                "last_error": "",
                "last_model_run_id": "",
                "last_n_pred": 0,
                "last_duration_ms": 0,
            },
        ]
    }


# ---- 通用策略权益 journal ----

@app.get("/api/v1/strategy/equity-journal")
async def strategy_equity_journal(
    engine: str,
    strategy_name: str,
    since: Optional[str] = Query(None),
    source_label: Optional[str] = Query(None),
    limit: int = Query(10000, ge=1, le=100000),
) -> List[Dict[str, Any]]:
    """直接读取 vnpy 端 strategy_equity_journal.db."""
    return list_snapshots(
        engine=engine,
        strategy_name=strategy_name,
        source_label=source_label,
        since_ts=since,
        limit=limit,
    )


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
    print("[fake_webtrader] /api/v1/strategy/equity-journal 直读 VNPY_DATA_ROOT/state/strategy_equity_journal.db")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
