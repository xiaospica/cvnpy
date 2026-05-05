"""ML 监控路由: ``/api/v1/ml/*``.

契约 4 (总方案 v4.1): 6 个端点, 均 GET, 复用 JWT + ApiResponse 信封.

路由表:

    GET /api/v1/ml/strategies/{name}/metrics/latest
    GET /api/v1/ml/strategies/{name}/metrics?days=30
    GET /api/v1/ml/strategies/{name}/prediction/latest/summary
    GET /api/v1/ml/strategies/{name}/prediction/{yyyymmdd}
    GET /api/v1/ml/strategies/{name}/replay/equity_snapshots?since=&limit=
    GET /api/v1/ml/health

下游:
    - mlearnweb ``ml_snapshot_loop`` 按 60s 轮询 metrics/latest, prediction/...
    - mlearnweb ``replay_equity_sync_service`` 按 5min 轮询 replay/equity_snapshots
      (增量 fanout 拉, A1/B2 解耦后 vnpy 不再直写 mlearnweb.db)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    days: int = Query(30, ge=1, le=1000),
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


@router.get("/strategies/{name}/prediction/dates")
def ml_prediction_dates(
    name: str,
    access: bool = Depends(get_access),
) -> List[str]:
    """列出策略可用预测日期 (升序 YYYY-MM-DD).

    Phase 3.2: 给 mlearnweb ``historical_predictions_sync`` 用 — 它会拿到这个
    列表后逐天 fetch_summary 灌进 SQLite, 解决 prediction/{yyyymmdd}/summary
    端点过去仅返最新一天的限制.
    """
    return unwrap_result(get_rpc_client().get_ml_prediction_dates(name))


@router.get("/strategies/{name}/prediction/{yyyymmdd}/summary")
def ml_prediction_summary_by_date(
    name: str,
    yyyymmdd: str,
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    """按日查询预测 summary (Phase 3.2 — 替代 2.7 的 501 stub).

    数据源: ``{output_root}/{name}/{yyyymmdd}/metrics.json`` + ``selections.parquet``,
    与 ``prediction/latest/summary`` 同结构 (含 topk + score_histogram +
    pred_mean/std + n_symbols + model_run_id).
    """
    return unwrap_result(get_rpc_client().get_ml_prediction_summary_by_date(name, yyyymmdd))


@router.get("/strategies/{name}/replay/equity_snapshots")
def ml_replay_equity_snapshots(
    name: str,
    since: Optional[str] = Query(
        None,
        description="ISO datetime; 仅返回 inserted_at >= since 的行 (增量同步用)",
    ),
    limit: int = Query(10000, ge=1, le=100000, description="单次返回最多行数"),
    access: bool = Depends(get_access),
) -> List[Dict[str, Any]]:
    """vnpy 端本地 replay_history.db 的回放权益快照 (A1/B2 解耦后新增).

    mlearnweb 端 ``replay_equity_sync_service`` 每 5 分钟通过 fanout 调本端点,
    用本地 ``MAX(inserted_at)`` 作 ``since`` 拉增量, UPSERT 到 mlearnweb.db
    的 ``strategy_equity_snapshots(source_label='replay_settle')``.

    返回空列表 = 该策略当前未发生过回放, 不算错误.
    """
    return unwrap_result(get_rpc_client().get_ml_replay_equity_snapshots(name, since, limit))


@router.get("/health")
def ml_health(access: bool = Depends(get_access)) -> Dict[str, Any]:
    """所有 ML 策略的最新存活/运行状态汇总."""
    return unwrap_result(get_rpc_client().get_ml_health())
