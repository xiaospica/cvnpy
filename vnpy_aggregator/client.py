"""单个节点的 HTTP + WS 客户端封装."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx
import websockets

from .config import NodeConfig


logger = logging.getLogger(__name__)

#: WS 事件回调签名: ``(node_id, message_dict) -> None``
EventHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]


@dataclass
class NodeState:
    node_id: str
    base_url: str
    online: bool = False
    last_heartbeat: float = 0.0
    consecutive_failures: int = 0
    info: Dict[str, Any] = field(default_factory=dict)
    health: Dict[str, Any] = field(default_factory=dict)


class NodeClient:
    """对单个节点的 REST + WS 访问封装. 缓存 token 并自动续期。"""

    def __init__(self, config: NodeConfig, ws_handler: Optional[EventHandler] = None) -> None:
        self.config = config
        self.state = NodeState(node_id=config.node_id, base_url=config.base_url)
        self._token: Optional[str] = None
        self._http = httpx.AsyncClient(
            base_url=config.base_url, verify=config.verify_tls, timeout=10.0
        )
        self._ws_handler = ws_handler
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_stop = asyncio.Event()

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

    async def close(self) -> None:
        self._ws_stop.set()
        if self._ws_task:
            self._ws_task.cancel()
        await self._http.aclose()

    async def login(self) -> str:
        resp = await self._http.post(
            "/api/v1/token",
            data={"username": self.config.username, "password": self.config.password},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _auth_headers(self) -> Dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._token:
            await self.login()
        headers = kwargs.pop("headers", {}) or {}
        headers.update(self._auth_headers())
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            await self.login()
            headers.update(self._auth_headers())
            resp = await self._http.request(method, path, headers=headers, **kwargs)
        return resp

    async def get_json(self, path: str, **kwargs) -> Any:
        resp = await self._request("GET", path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def post_json(self, path: str, json_body: Optional[dict] = None) -> Any:
        resp = await self._request("POST", path, json=json_body)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def forward(self, method: str, path: str, json_body: Any = None) -> Any:
        """透传任意方法/路径, 返回 (status_code, json|text)."""
        resp = await self._request(method, path, json=json_body)
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return resp.status_code, body

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    async def heartbeat(self) -> bool:
        try:
            info = await self.get_json("/api/v1/node/info")
            health = await self.get_json("/api/v1/node/health")
            self.state.info = info
            self.state.health = health
            self.state.last_heartbeat = time.time()
            self.state.online = True
            self.state.consecutive_failures = 0
            return True
        except Exception as exc:
            self.state.consecutive_failures += 1
            logger.warning("[%s] heartbeat failed: %s", self.config.node_id, exc)
            return False

    def mark_offline_if_needed(self, threshold: int) -> None:
        if self.state.consecutive_failures >= threshold:
            self.state.online = False

    # ------------------------------------------------------------------
    # WebSocket 上游订阅
    # ------------------------------------------------------------------

    async def start_ws(self) -> None:
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_stop.clear()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        """持续订阅节点 WS, 断线自动重连。"""
        base = self.config.base_url
        ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
        while not self._ws_stop.is_set():
            try:
                if not self._token:
                    await self.login()
                url = f"{ws_url}/api/v1/ws?token={self._token}"
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("[%s] ws connected", self.config.node_id)
                    while not self._ws_stop.is_set():
                        raw = await ws.recv()
                        if not self._ws_handler:
                            continue
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        msg["node_id"] = self.config.node_id
                        try:
                            await self._ws_handler(self.config.node_id, msg)
                        except Exception as exc:  # pragma: no cover
                            logger.exception("ws handler error: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[%s] ws loop error: %s, retry in 5s", self.config.node_id, exc)
                await asyncio.sleep(5)
