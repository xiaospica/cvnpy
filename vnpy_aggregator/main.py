"""聚合中控 FastAPI 入口."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from .auth import (
    authenticate_admin,
    create_access_token,
    require_user,
    set_config as set_auth_config,
)
from .config import AggregatorConfig, NodeConfig, load_config
from .registry import NodeRegistry
from .ws_hub import WsHub


logging.basicConfig(level=logging.INFO)

app = FastAPI(title="vnpy_aggregator", version="0.1.0")

_config: Optional[AggregatorConfig] = None
_registry: Optional[NodeRegistry] = None
_hub = WsHub()


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AddNodeBody(BaseModel):
    node_id: str
    base_url: str
    username: str = "vnpy"
    password: str = "vnpy"
    verify_tls: bool = True


class FanoutItem(BaseModel):
    node_id: str
    ok: bool
    data: Any = None
    error: Optional[str] = None


class NodeDescribe(BaseModel):
    node_id: str
    base_url: str
    online: bool
    last_heartbeat: float
    info: Dict[str, Any] = Field(default_factory=dict)
    health: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 启停
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    global _config, _registry
    _config = load_config()
    set_auth_config(_config)
    _registry = NodeRegistry(_config, ws_dispatch=_hub.dispatch)
    await _registry.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _registry is not None:
        await _registry.stop()


def _reg() -> NodeRegistry:
    if _registry is None:
        raise HTTPException(status_code=503, detail="registry not ready")
    return _registry


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


@app.post("/agg/token", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends()) -> Dict[str, str]:  # noqa: B008
    user = authenticate_admin(form.username, form.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": create_access_token(user), "token_type": "bearer"}


# ---------------------------------------------------------------------------
# 节点管理
# ---------------------------------------------------------------------------


@app.get("/agg/nodes", response_model=List[NodeDescribe])
async def list_nodes(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return _reg().describe_nodes()


@app.post("/agg/nodes")
async def add_node(body: AddNodeBody, user: str = Depends(require_user)) -> Dict[str, Any]:
    await _reg().add_node(
        NodeConfig(
            node_id=body.node_id,
            base_url=body.base_url.rstrip("/"),
            username=body.username,
            password=body.password,
            verify_tls=body.verify_tls,
        )
    )
    return {"ok": True}


@app.delete("/agg/nodes/{node_id}")
async def delete_node(node_id: str, user: str = Depends(require_user)) -> Dict[str, Any]:
    ok = await _reg().remove_node(node_id)
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 扇出只读接口
# ---------------------------------------------------------------------------


_FAST_FANOUT_TIMEOUT = httpx.Timeout(connect=1.5, read=3.0, write=3.0, pool=3.0)


async def _fanout(path: str) -> List[Dict[str, Any]]:
    return await _reg().fanout_get(path, timeout=_FAST_FANOUT_TIMEOUT)


@app.get("/agg/accounts", response_model=List[FanoutItem])
async def agg_accounts(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/account")


@app.get("/agg/positions", response_model=List[FanoutItem])
async def agg_positions(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/position")


@app.get("/agg/orders", response_model=List[FanoutItem])
async def agg_orders(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/order")


@app.get("/agg/trades", response_model=List[FanoutItem])
async def agg_trades(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/trade")


@app.get("/agg/strategies", response_model=List[FanoutItem])
async def agg_strategies(user: str = Depends(require_user)) -> List[Dict[str, Any]]:
    return await _fanout("/api/v1/strategy")


# ---------------------------------------------------------------------------
# 节点透传 (写操作)
# ---------------------------------------------------------------------------


@app.api_route(
    "/agg/nodes/{node_id}/proxy/{path:path}",
    methods=["GET", "POST", "DELETE", "PATCH"],
)
async def proxy(
    node_id: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    user: str = Depends(require_user),
) -> Any:
    """把请求透传到指定节点 ``/api/v1/{path}``。"""
    client = _reg().get(node_id)
    if client is None:
        raise HTTPException(status_code=404, detail="node not found")
    from fastapi import Request  # noqa: local import to avoid cycle in type hints

    # 直接转发 - 简化版: 由前端构造完整 sub-path
    target = f"/api/v1/{path}"
    try:
        status_code, body = await client.forward(
            "POST" if payload else "GET",
            target,
            payload,
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=8.0),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=body)
    return body


# ---------------------------------------------------------------------------
# WebSocket 入口
# ---------------------------------------------------------------------------


@app.websocket("/agg/ws")
async def agg_ws(websocket: WebSocket) -> None:
    """前端订阅入口. token 通过 query string 传入。"""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        await require_user(token)  # type: ignore[arg-type]
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await _hub.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await _hub.remove(websocket)
