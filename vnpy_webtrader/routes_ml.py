"""ML 监控路由: ``/api/v1/ml/*``.

契约 4 (总方案 v4.1): 5 个端点, 均 GET, 复用 JWT + ApiResponse 信封.

路由表:

    GET /api/v1/ml/strategies/{name}/metrics/latest
    GET /api/v1/ml/strategies/{name}/metrics?days=30
    GET /api/v1/ml/strategies/{name}/prediction/latest/summary
    GET /api/v1/ml/strategies/{name}/prediction/{yyyymmdd}
    GET /api/v1/ml/health

下游 (mlearnweb 的 ``ml_snapshot_loop``) 按此约定每 60s 轮询.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query

from .deps import get_access, get_rpc_client, unwrap_result


router = APIRouter(prefix="/api/v1/ml", tags=["ml"])


@router.get("/strategies/{name}/metrics/latest")
def ml_metrics_latest(
    name: str,
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    """最新一日监控指标 (来自 MetricsCache 内存缓存 + 子进程 metrics.json)."""
    return unwrap_result(get_rpc_client().get_ml_metrics_latest(name))


@router.get("/strategies/{name}/metrics")
def ml_metrics_history(
    name: str,
    days: int = Query(30, ge=1, le=365),
    access: bool = Depends(get_access),
) -> List[Dict[str, Any]]:
    """最近 N 日指标列表 (ring buffer 按插入顺序返回)."""
    return unwrap_result(get_rpc_client().get_ml_metrics_history(name, days))


@router.get("/strategies/{name}/prediction/latest/summary")
def ml_prediction_latest_summary(
    name: str,
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    """最新一日预测 summary: topk + histogram + n_symbols + coverage."""
    return unwrap_result(get_rpc_client().get_ml_prediction_summary(name))


@router.get("/strategies/{name}/prediction/{yyyymmdd}")
def ml_prediction_by_date(
    name: str,
    yyyymmdd: str,
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    """按日查询预测 summary.

    Phase 2.6: 从磁盘读 ``{output_root}/{name}/{yyyymmdd}/metrics.json``. 当前
    走 rpc_client 的 get_ml_prediction_summary 兜底返回 latest (非按日);
    按日查询留待 Phase 2.7 接入磁盘扫描.
    """
    # TODO Phase 2.7: via get_rpc_client().get_ml_prediction_by_date(name, yyyymmdd)
    raise HTTPException(status_code=501, detail="按日查询待 Phase 2.7 实现")


@router.get("/health")
def ml_health(access: bool = Depends(get_access)) -> Dict[str, Any]:
    """所有 ML 策略的最新存活/运行状态汇总."""
    return unwrap_result(get_rpc_client().get_ml_health())
