"""通用策略管理路由: ``/api/v1/strategy/*``.

所有请求都通过 ``WebEngine`` 暴露的 RPC 方法转发, 由后端的 ``StrategyEngineAdapter``
屏蔽不同策略引擎的接口差异。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .deps import get_access, get_fast_rpc_client, get_rpc_client, unwrap_result


router = APIRouter(prefix="/api/v1/strategy", tags=["strategy"])


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------


class EngineDescription(BaseModel):
    app_name: str
    display_name: str
    event_type: str
    capabilities: List[str]


class StrategyInfoModel(BaseModel):
    engine: str
    name: str
    class_name: str
    vt_symbol: Optional[str] = None
    author: Optional[str] = None
    inited: bool
    trading: bool
    parameters: Dict[str, Any] = Field(default_factory=dict)
    variables: Dict[str, Any] = Field(default_factory=dict)


class AddStrategyBody(BaseModel):
    class_name: str
    strategy_name: str
    vt_symbol: Optional[str] = None
    setting: Dict[str, Any] = Field(default_factory=dict)


class EditStrategyBody(BaseModel):
    setting: Dict[str, Any] = Field(default_factory=dict)


class OpResultModel(BaseModel):
    ok: bool = True
    message: str = ""
    data: Any = None


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------


@router.get("/engines", response_model=List[EngineDescription])
def list_engines(access: bool = Depends(get_access)) -> List[Dict[str, Any]]:
    return get_fast_rpc_client().list_strategy_engines()


@router.get("/engines/{engine}/classes")
def list_classes(engine: str, access: bool = Depends(get_access)) -> List[str]:
    return unwrap_result(get_fast_rpc_client().list_strategy_classes(engine))


@router.get("/engines/{engine}/classes/{class_name}/params")
def get_class_params(
    engine: str, class_name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return unwrap_result(get_fast_rpc_client().get_strategy_class_params(engine, class_name))


@router.get("", response_model=List[StrategyInfoModel])
def list_all_strategies(access: bool = Depends(get_access)) -> List[Dict[str, Any]]:
    return unwrap_result(get_fast_rpc_client().list_strategies(""))


@router.get("/engines/{engine}", response_model=List[StrategyInfoModel])
def list_engine_strategies(
    engine: str, access: bool = Depends(get_access)
) -> List[Dict[str, Any]]:
    return unwrap_result(get_fast_rpc_client().list_strategies(engine))


@router.get(
    "/engines/{engine}/instances/{name}", response_model=StrategyInfoModel
)
def get_strategy(
    engine: str, name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return unwrap_result(get_fast_rpc_client().get_strategy(engine, name))


# ---------------------------------------------------------------------------
# 写接口
# ---------------------------------------------------------------------------


@router.post("/engines/{engine}/instances", response_model=OpResultModel)
def add_strategy(
    engine: str, body: AddStrategyBody, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    payload = body.dict()
    payload["engine"] = engine
    return get_rpc_client().add_strategy(payload)


@router.post(
    "/engines/{engine}/instances/{name}/init", response_model=OpResultModel
)
def init_strategy(
    engine: str, name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return get_rpc_client().init_strategy(engine, name)


@router.post(
    "/engines/{engine}/instances/{name}/start", response_model=OpResultModel
)
def start_strategy(
    engine: str, name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return get_rpc_client().start_strategy(engine, name)


@router.post(
    "/engines/{engine}/instances/{name}/stop", response_model=OpResultModel
)
def stop_strategy(
    engine: str, name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return get_rpc_client().stop_strategy(engine, name)


@router.delete(
    "/engines/{engine}/instances/{name}", response_model=OpResultModel
)
def remove_strategy(
    engine: str, name: str, access: bool = Depends(get_access)
) -> Dict[str, Any]:
    return get_rpc_client().remove_strategy(engine, name)


@router.patch(
    "/engines/{engine}/instances/{name}", response_model=OpResultModel
)
def edit_strategy(
    engine: str,
    name: str,
    body: EditStrategyBody,
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    return get_rpc_client().edit_strategy(engine, name, body.setting or {})


@router.post(
    "/engines/{engine}/actions/init-all", response_model=OpResultModel
)
def init_all(engine: str, access: bool = Depends(get_access)) -> Dict[str, Any]:
    return get_rpc_client().init_all_strategies(engine)


@router.post(
    "/engines/{engine}/actions/start-all", response_model=OpResultModel
)
def start_all(engine: str, access: bool = Depends(get_access)) -> Dict[str, Any]:
    return get_rpc_client().start_all_strategies(engine)


@router.post(
    "/engines/{engine}/actions/stop-all", response_model=OpResultModel
)
def stop_all(engine: str, access: bool = Depends(get_access)) -> Dict[str, Any]:
    return get_rpc_client().stop_all_strategies(engine)
