"""聚合层独立 JWT 鉴权 (与节点层凭据解耦)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import AggregatorConfig


pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/agg/token")

_config: Optional[AggregatorConfig] = None


def set_config(cfg: AggregatorConfig) -> None:
    global _config
    _config = cfg


def _cfg() -> AggregatorConfig:
    if _config is None:
        raise RuntimeError("aggregator config not initialized")
    return _config


def authenticate_admin(username: str, password: str) -> Optional[str]:
    cfg = _cfg()
    if not secrets.compare_digest(username, cfg.admin_username):
        return None
    if not secrets.compare_digest(password, cfg.admin_password):
        return None
    return username


def create_access_token(sub: str) -> str:
    cfg = _cfg()
    expire = datetime.utcnow() + timedelta(minutes=cfg.token_expire_minutes)
    return jwt.encode({"sub": sub, "exp": expire}, cfg.jwt_secret, algorithm="HS256")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_user(token: str = Depends(oauth2_scheme)) -> str:
    cfg = _cfg()
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=["HS256"])
        sub = payload.get("sub")
    except JWTError as err:
        raise _unauthorized() from err
    if not sub:
        raise _unauthorized()
    return sub
