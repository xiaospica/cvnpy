"""FastAPI Web 进程入口.

职责:
    1. 加载 ``web_trader_setting.json`` 配置并创建 ``RpcClient`` 连接交易进程;
    2. 暴露交易相关 REST 接口 (account/order/position/...);
    3. 通过 ``include_router`` 挂载策略管理和节点自描述路由;
    4. 通过 WebSocket 把 RPC 推送转发给浏览器, 消息结构为
       ``{"topic": "...", "engine": "...", "node_id": "...", "data": {...}}``。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from vnpy.rpc import RpcClient
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.event import (
    EVENT_ACCOUNT,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_POSITION,
    EVENT_TICK,
    EVENT_TRADE,
)
from vnpy.trader.object import (
    AccountData,
    CancelRequest,
    ContractData,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
    TradeData,
)

from .deps import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    NODE_DISPLAY,
    NODE_ID,
    REQ_ADDRESS,
    SUB_ADDRESS,
    Token,
    authenticate_user,
    create_access_token,
    get_access,
    get_rpc_client,
    get_websocket_access,
    set_rpc_client,
    to_dict,
)
from .routes_node import router as node_router
from .routes_strategy import router as strategy_router


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

app: FastAPI = FastAPI(title="vnpy_webtrader", version="1.2.0")
app.include_router(node_router)
app.include_router(strategy_router)


# ---------------------------------------------------------------------------
# 页面入口 / 登录
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> HTMLResponse:
    index_path = Path(__file__).parent.joinpath("static/index.html")
    if not index_path.exists():
        return HTMLResponse("<h1>vnpy_webtrader</h1><p>REST API ready, see /docs.</p>")
    with open(index_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/v1/token", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends()) -> Dict[str, str]:  # noqa: B008
    username = authenticate_user(form_data.username, form_data.password)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": token, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# 交易类 REST
# ---------------------------------------------------------------------------


class OrderRequestModel(BaseModel):
    symbol: str
    exchange: Exchange
    direction: Direction
    type: OrderType
    volume: float
    price: float = 0
    offset: Offset = Offset.NONE
    reference: str = ""


@app.post("/api/v1/tick/{vt_symbol}")
def subscribe(vt_symbol: str, access: bool = Depends(get_access)) -> None:
    rpc = get_rpc_client()
    contract: ContractData | None = rpc.get_contract(vt_symbol)
    if not contract:
        raise HTTPException(status_code=404, detail=f"找不到合约{vt_symbol}")
    req = SubscribeRequest(contract.symbol, contract.exchange)
    rpc.subscribe(req, contract.gateway_name)


@app.get("/api/v1/tick")
def get_all_ticks(access: bool = Depends(get_access)) -> List[dict]:
    ticks: List[TickData] = get_rpc_client().get_all_ticks()
    return [to_dict(t) for t in ticks]


@app.post("/api/v1/order")
def send_order(model: OrderRequestModel, access: bool = Depends(get_access)) -> str:
    rpc = get_rpc_client()
    req = OrderRequest(**model.dict())
    contract: ContractData | None = rpc.get_contract(req.vt_symbol)
    if not contract:
        raise HTTPException(
            status_code=404, detail=f"找不到合约{req.symbol} {req.exchange.value}"
        )
    return rpc.send_order(req, contract.gateway_name)


@app.delete("/api/v1/order/{vt_orderid}")
def cancel_order(vt_orderid: str, access: bool = Depends(get_access)) -> None:
    rpc = get_rpc_client()
    order: OrderData | None = rpc.get_order(vt_orderid)
    if not order:
        raise HTTPException(status_code=404, detail=f"找不到委托{vt_orderid}")
    req: CancelRequest = order.create_cancel_request()
    rpc.cancel_order(req, order.gateway_name)


@app.get("/api/v1/order")
def get_all_orders(access: bool = Depends(get_access)) -> List[dict]:
    orders: List[OrderData] = get_rpc_client().get_all_orders()
    return [to_dict(o) for o in orders]


@app.get("/api/v1/trade")
def get_all_trades(access: bool = Depends(get_access)) -> List[dict]:
    trades: List[TradeData] = get_rpc_client().get_all_trades()
    return [to_dict(t) for t in trades]


@app.get("/api/v1/position")
def get_all_positions(access: bool = Depends(get_access)) -> List[dict]:
    positions: List[PositionData] = get_rpc_client().get_all_positions()
    return [to_dict(p) for p in positions]


@app.get("/api/v1/account")
def get_all_accounts(access: bool = Depends(get_access)) -> List[dict]:
    accounts: List[AccountData] = get_rpc_client().get_all_accounts()
    return [to_dict(a) for a in accounts]


@app.get("/api/v1/contract")
def get_all_contracts(access: bool = Depends(get_access)) -> List[dict]:
    contracts: List[ContractData] = get_rpc_client().get_all_contracts()
    return [to_dict(c) for c in contracts]


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

active_websockets: List[WebSocket] = []
event_loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()


#: 交易进程原始 topic -> 对外语义化 topic
_BASE_TOPIC_MAP: Dict[str, str] = {
    EVENT_TICK: "tick",
    EVENT_ORDER: "order",
    EVENT_TRADE: "trade",
    EVENT_POSITION: "position",
    EVENT_ACCOUNT: "account",
    EVENT_LOG: "log",
}

#: 策略引擎事件 topic -> app_name, 启动后由 list_strategy_engines 填充
_STRATEGY_TOPIC_MAP: Dict[str, str] = {}


@app.websocket("/api/v1/ws")
async def websocket_endpoint(
    websocket: WebSocket, access: bool = Depends(get_websocket_access)
) -> None:
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)


async def _broadcast(msg: str) -> None:
    dead: List[WebSocket] = []
    for ws in active_websockets:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in active_websockets:
            active_websockets.remove(ws)


def _map_topic(raw_topic: str) -> tuple[str, str]:
    """根据原始事件 topic 得到 (wire_topic, engine_app_name)."""
    # 精确命中: 基础事件
    for raw, wire in _BASE_TOPIC_MAP.items():
        if raw_topic == raw or raw_topic.startswith(raw):
            return wire, ""
    # 策略事件
    engine = _STRATEGY_TOPIC_MAP.get(raw_topic, "")
    if engine:
        return "strategy", engine
    return raw_topic, ""


def _rpc_callback(topic: str, data: Any) -> None:
    """RpcClient 接收到推送时触发, 打包成 WS 消息广播给所有前端连接。"""
    if not active_websockets:
        return
    wire_topic, engine = _map_topic(topic)
    message: Dict[str, Any] = {
        "topic": wire_topic,
        "node_id": NODE_ID,
        "ts": time.time(),
        "data": to_dict(data) if hasattr(data, "__dict__") else data,
    }
    if engine:
        message["engine"] = engine
    try:
        payload = json.dumps(message, ensure_ascii=False, default=str)
    except Exception:
        return
    asyncio.run_coroutine_threadsafe(_broadcast(payload), event_loop)


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _startup() -> None:
    client = RpcClient()
    client.callback = _rpc_callback
    client.subscribe_topic("")
    client.start(REQ_ADDRESS, SUB_ADDRESS)
    set_rpc_client(client)

    # 拉取策略引擎列表, 构建 event topic -> app_name 映射
    try:
        engines = client.list_strategy_engines()
        for item in engines or []:
            ev = item.get("event_type")
            app_name = item.get("app_name")
            if ev and app_name:
                _STRATEGY_TOPIC_MAP[ev] = app_name
    except Exception:
        # 交易进程可能未实现这些方法, 容忍降级
        pass


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        get_rpc_client().stop()
    except Exception:
        pass


@app.get("/api/v1/node/id")
def node_id_debug() -> Dict[str, str]:
    """便捷调试接口: 返回本节点 id (无需鉴权)."""
    return {"node_id": NODE_ID, "display_name": NODE_DISPLAY}
