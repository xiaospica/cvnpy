"""市场参考数据路由: ``/api/v1/reference/*``.

给 mlearnweb 监控端用的"全市场静态数据"端点 — 不绑特定策略, 不依赖
EventEngine, 直接从 vnpy_tushare_pro 的 stock_list.parquet 读出来返还.

设计原则:
    mlearnweb 跨机部署不应假设能访问 vnpy 推理机的文件系统; 这些原本由
    mlearnweb 直读 ``F:/Quant/vnpy/vnpy_strategy_dev/stock_data/stock_list.parquet``
    的逻辑迁移到 HTTP, 监控端只走 webtrader.

路由:
    GET /api/v1/reference/stock_names — 全市场 ts_code → 中文简称字典

下游:
    mlearnweb ``ml_aggregation_service.get_stock_name_map`` 1h 内存缓存,
    HTTP 失败时返回空 dict 让前端 fallback 到显示 ts_code.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

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
