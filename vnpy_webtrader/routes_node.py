"""节点自描述路由: ``/api/v1/node/*``.

聚合层根据这里返回的数据识别节点身份、渲染节点列表、做健康检查。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .deps import get_access, get_rpc_client


router = APIRouter(prefix="/api/v1/node", tags=["node"])


@router.get("/info")
def node_info(access: bool = Depends(get_access)) -> Dict[str, Any]:
    """节点元信息: 身份, gateway 列表, 已加载的 app engine, 策略引擎能力。"""
    return get_rpc_client().get_node_info()


@router.get("/health")
def node_health(access: bool = Depends(get_access)) -> Dict[str, Any]:
    """节点健康状态: uptime, event queue 深度, gateway 连通性。"""
    return get_rpc_client().get_node_health()
