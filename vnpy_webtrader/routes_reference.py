"""市场参考数据路由: ``/api/v1/reference/*``.

给 mlearnweb 监控端用的"全市场静态数据"端点 — 不绑特定策略, 不依赖
EventEngine, 直接从 vnpy_tushare_pro 的 stock_list.parquet / 本地
``snapshots/merged/daily_merged_{T}.parquet`` 读出来返还.

设计原则:
    mlearnweb 跨机部署不应假设能访问 vnpy 推理机的文件系统; 这些原本由
    mlearnweb 直读本地 parquet 的逻辑迁移到 HTTP, 监控端只走 webtrader.

路由:
    GET /api/v1/reference/stock_names    — 全市场 ts_code → 中文简称字典 (Phase 3.1)
    GET /api/v1/reference/corp_actions   — 持仓最近 N 日除权事件 (Phase 3.3)

下游:
    mlearnweb ``stock_name_cache`` 1h 内存缓存, HTTP 失败时返空 dict 让前端
    fallback 显示 ts_code.
    mlearnweb ``corp_actions_service`` Phase 3.3 后退化为 HTTP 客户端,
    detect_corp_actions 算法搬到本端进程, 直接读 vnpy 推理机本地 parquet.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from .deps import get_access


router = APIRouter(prefix="/api/v1/reference", tags=["reference"])


@router.get("/stock_names")
def stock_names(access: bool = Depends(get_access)) -> Dict[str, Any]:
    """全市场 ts_code → 中文简称字典.

    数据源: ``vnpy_tushare_pro/ml_data_build/stock_name_lookup.py`` 的进程级
    StockNameLookup singleton (内部读 stock_list.parquet, mtime/TTL 自动刷新).

    Returns
    -------
    {
        "names": {"000001.SZ": "平安银行", "600000.SH": "浦发银行", ...},
        "count": 5234,
        "source_path": "/path/to/stock_list.parquet",  # debug 用, 可能 None
    }

    parquet 缺失时 ``names`` 为空 dict, ``count=0``, ``source_path`` 为 None;
    mlearnweb 端拉到空字典自然 fallback 到显示 ts_code, 不报错.
    """
    from vnpy_tushare_pro.ml_data_build.stock_name_lookup import (
        get_default_lookup,
    )

    lookup = get_default_lookup()
    # 触发 lazy 加载 + mtime/TTL 刷新
    lookup.refresh(force=False)
    # 读私有字段 _mapping 为了避开 enrich 的 DataFrame 接口 — 我们只要纯 dict
    # (返回 dict copy 防止外部并发修改 cache).
    with lookup._lock:
        mapping_copy: Dict[str, str] = dict(lookup._mapping)
        source_path = lookup._resolve_path()
    return {
        "names": mapping_copy,
        "count": len(mapping_copy),
        "source_path": str(source_path) if source_path is not None else None,
    }


@router.get("/corp_actions")
def corp_actions(
    vt_symbols: str = Query(
        ...,
        description="逗号分隔 vt_symbol 列表 (如 000001.SZSE,600519.SSE)",
    ),
    days: int = Query(30, ge=1, le=180, description="向前回溯天数"),
    threshold_pct: float = Query(
        0.5, ge=0.0, le=20.0,
        description="pct_chg 与原始 close 涨跌幅差异阈值 (%) — 超过判定为除权日",
    ),
    access: bool = Depends(get_access),
) -> Dict[str, Any]:
    """检测最近 N 日内持仓股票发生的除权除息事件 (Phase 3.3).

    数据源: 本节点 ``${ML_SNAPSHOT_DIR | QS_DATA_ROOT/snapshots}/merged/daily_merged_{T}.parquet``
    (vnpy_tushare_pro DailyIngestPipeline 每日 20:00 落盘).

    Returns
    -------
    {
        "events": [
            {"vt_symbol": "...", "name": "...", "trade_date": "YYYY-MM-DD",
             "pct_chg": float, "raw_change_pct": float, "magnitude_pct": float,
             "pre_close": float, "close": float},
            ...
        ],
        "count": int,
        "as_of": "YYYY-MM-DD",  # 用的是哪天的快照
        "snapshot_path": str | None,  # 快照绝对路径 (debug 用); None=未找到
    }

    snapshot 缺失时 ``events`` 为空列表 (不抛 500), mlearnweb 显示"暂无事件"
    即可, 不影响其他持仓页面.
    """
    from datetime import date as _date
    from ._corp_actions import (
        detect_corp_actions,
        serialize_events,
        _resolve_merged_snapshot,
    )

    symbols = [s.strip() for s in vt_symbols.split(",") if s.strip()]
    if not symbols:
        return {"events": [], "count": 0, "as_of": _date.today().isoformat(), "snapshot_path": None}

    today = _date.today()
    events = detect_corp_actions(
        symbols, as_of=today, lookback_days=days, threshold_pct=threshold_pct,
    )
    snap = _resolve_merged_snapshot(today)
    return {
        "events": serialize_events(events),
        "count": len(events),
        "as_of": today.isoformat(),
        "snapshot_path": str(snap) if snap is not None else None,
    }
