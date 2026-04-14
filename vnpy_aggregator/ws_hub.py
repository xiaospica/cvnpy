"""聚合层 WS 中心. 前端只和这里建立一条 WS, 它从 NodeRegistry 汇聚节点事件后广播."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import WebSocket


logger = logging.getLogger(__name__)


class WsHub:
    """单例 WebSocket 广播器. 由 FastAPI 应用启动时创建."""

    def __init__(self) -> None:
        self._clients: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.append(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._clients:
                self._clients.remove(ws)

    async def dispatch(self, node_id: str, message: Dict[str, Any]) -> None:
        """由 NodeRegistry 的 ws_dispatch 回调调用。"""
        message.setdefault("node_id", node_id)
        payload = json.dumps(message, ensure_ascii=False, default=str)
        await self._broadcast(payload)

    async def _broadcast(self, payload: str) -> None:
        dead: List[WebSocket] = []
        async with self._lock:
            for ws in list(self._clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in self._clients:
                    self._clients.remove(ws)
