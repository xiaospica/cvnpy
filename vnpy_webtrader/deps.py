"""共享依赖: RPC 客户端持有、JWT 鉴权、序列化工具。

拆分出来是为了让多个 route 模块 (``routes_node`` / ``routes_strategy``) 复用,
同时避免与主模块 ``web.py`` 产生循环导入。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from fastapi import Depends, HTTPException, Query, WebSocket, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from vnpy.rpc import RpcClient
from vnpy.trader.utility import get_file_path, load_json


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

SETTING_FILENAME = "web_trader_setting.json"
SETTING_FILEPATH = get_file_path(SETTING_FILENAME)

_setting: dict = load_json(SETTING_FILEPATH) or {}
USERNAME: str = _setting.get("username", "vnpy")
PASSWORD: str = _setting.get("password", "vnpy")
# 端口支持环境变量覆盖（向后兼容：env 未设时回退到 setting.json，再回退到默认值）。
# 用途：同一台机器上可能同时跑生产 webtrader（默认 2014/4102）和 e2e 测试 sim
# （test_setting.json 用 12014/14102），通过 env 把测试 uvicorn 子进程指向测试 RpcServer，
# 不污染 .vntrader/web_trader_setting.json。
REQ_ADDRESS: str = os.environ.get("VNPY_WEB_REQ_ADDRESS") or _setting.get(
    "req_address", "tcp://127.0.0.1:2014"
)
SUB_ADDRESS: str = os.environ.get("VNPY_WEB_SUB_ADDRESS") or _setting.get(
    "sub_address", "tcp://127.0.0.1:4102"
)
NODE_ID: str = _setting.get("node_id", "") or os.environ.get("VNPY_NODE_ID", "unnamed")
NODE_DISPLAY: str = _setting.get("display_name", "") or NODE_ID

SECRET_KEY: str = os.environ.get("VNPY_WEB_SECRET", _setting.get("secret_key", "change-me"))
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(_setting.get("token_expire_minutes", 30))


# ---------------------------------------------------------------------------
# 鉴权工具
# ---------------------------------------------------------------------------

pwd_context: CryptContext = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme: OAuth2PasswordBearer = OAuth2PasswordBearer(tokenUrl="/api/v1/token")


class Token(BaseModel):
    access_token: str
    token_type: str


def authenticate_user(username: str, password: str) -> Optional[str]:
    """校验用户名密码, 通过返回 username, 否则返回 None."""
    if not secrets.compare_digest(USERNAME, username):
        return None
    hashed = pwd_context.hash(PASSWORD)
    if not pwd_context.verify(password, hashed):
        return None
    return username


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_access(token: str = Depends(oauth2_scheme)) -> bool:
    """REST 鉴权依赖。"""
    exc = _credentials_exception()
    try:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError as err:
        raise exc from err
    if not username or not secrets.compare_digest(USERNAME, username):
        raise exc
    return True


async def get_websocket_access(
    websocket: WebSocket, token: Optional[str] = Query(None)
) -> bool:
    """Websocket 鉴权依赖。"""
    exc = _credentials_exception()
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        raise exc
    try:
        payload: dict = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
    except JWTError as err:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        raise exc from err
    if not username or not secrets.compare_digest(USERNAME, username):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        raise exc
    return True


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def to_dict(o: Any) -> dict:
    """把 vnpy dataclass 对象转成 JSON-friendly dict."""
    if isinstance(o, dict):
        return {k: _encode_value(v) for k, v in o.items()}
    result: dict = {}
    for k, v in vars(o).items():
        result[k] = _encode_value(v)
    return result


def _encode_value(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# ---------------------------------------------------------------------------
# RPC 客户端单例
# ---------------------------------------------------------------------------

_rpc_client: Optional[RpcClient] = None


def set_rpc_client(client: RpcClient) -> None:
    global _rpc_client
    _rpc_client = client


def get_rpc_client() -> RpcClient:
    if _rpc_client is None:
        raise HTTPException(status_code=503, detail="RPC client not initialized")
    return _rpc_client


# ---------------------------------------------------------------------------
# 统一错误响应处理
# ---------------------------------------------------------------------------


def unwrap_result(result: Any) -> Any:
    """把 WebEngine 返回的统一 envelope ``{ok,message,data}`` 解包。

    - ``ok=True`` 时返回 ``data``
    - ``ok=False`` 时根据 ``data.http_status`` 抛 HTTPException
    - 非 envelope 直接原样返回 (例如 list/None)
    """
    if not isinstance(result, dict) or "ok" not in result:
        return result
    if result.get("ok"):
        return result.get("data")

    data = result.get("data") or {}
    http_status = 400
    if isinstance(data, dict):
        http_status = int(data.get("http_status", 400))
    raise HTTPException(status_code=http_status, detail=result.get("message", "operation failed"))
