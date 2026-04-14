"""节点注册表 + 心跳任务 + WS 汇流."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from .client import NodeClient
from .config import AggregatorConfig, NodeConfig


logger = logging.getLogger(__name__)


class NodeRegistry:
    """负责节点生命周期, 心跳, 与 WS 汇流到 ``WsHub``."""

    def __init__(
        self,
        config: AggregatorConfig,
        ws_dispatch: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> None:
        self.config = config
        self._clients: Dict[str, NodeClient] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._ws_dispatch = ws_dispatch

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        for node in self.config.nodes:
            await self.add_node(node)
        self._stop.clear()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_node(self, node: NodeConfig) -> None:
        if node.node_id in self._clients:
            return
        client = NodeClient(node, ws_handler=self._handle_ws)
        self._clients[node.node_id] = client
        try:
            await client.login()
            await client.heartbeat()
            await client.start_ws()
        except Exception as exc:
            logger.warning("add_node %s initial login failed: %s", node.node_id, exc)

    async def remove_node(self, node_id: str) -> bool:
        client = self._clients.pop(node_id, None)
        if client is None:
            return False
        await client.close()
        return True

    def get(self, node_id: str) -> Optional[NodeClient]:
        return self._clients.get(node_id)

    def all(self) -> List[NodeClient]:
        return list(self._clients.values())

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            for client in list(self._clients.values()):
                await client.heartbeat()
                client.mark_offline_if_needed(self.config.heartbeat_fail_threshold)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # WS 汇流
    # ------------------------------------------------------------------

    async def _handle_ws(self, node_id: str, message: Dict[str, Any]) -> None:
        if self._ws_dispatch is None:
            return
        await self._ws_dispatch(node_id, message)

    # ------------------------------------------------------------------
    # 扇出
    # ------------------------------------------------------------------

    async def fanout_get(self, path: str) -> List[Dict[str, Any]]:
        """对所有在线节点并发执行 GET, 返回 ``[{node_id, ok, data}]``."""
        async def _one(client: NodeClient) -> Dict[str, Any]:
            if not client.state.online:
                return {"node_id": client.config.node_id, "ok": False, "data": None, "error": "offline"}
            try:
                data = await client.get_json(path)
                return {"node_id": client.config.node_id, "ok": True, "data": data}
            except Exception as exc:
                return {"node_id": client.config.node_id, "ok": False, "error": str(exc)}

        tasks = [_one(c) for c in self._clients.values()]
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    def describe_nodes(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for c in self._clients.values():
            result.append({
                "node_id": c.config.node_id,
                "base_url": c.config.base_url,
                "online": c.state.online,
                "last_heartbeat": c.state.last_heartbeat,
                "info": c.state.info,
                "health": c.state.health,
            })
        return result
