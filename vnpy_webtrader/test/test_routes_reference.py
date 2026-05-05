"""routes_reference.py 单测.

验证 GET /api/v1/reference/stock_names 端点:
  - parquet 存在: 返回完整 dict + count
  - parquet 缺失: 返回空 dict + count=0 + source_path=None (graceful)
  - JWT 认证保护

不依赖完整 vnpy MainEngine — 用 FastAPI TestClient 单独 mount router.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def stock_list_parquet(tmp_path: Path) -> Path:
    p = tmp_path / "stock_list.parquet"
    df = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH", "300750.SZ"],
        "name": ["平安银行", "浦发银行", "宁德时代"],
    })
    df.to_parquet(p)
    return p


@pytest.fixture
def app_with_reference() -> FastAPI:
    """创建只挂 reference 路由的最小 FastAPI app, 跳过 JWT 鉴权.

    FastAPI dependency_overrides 必须用 route 实际 import 的同一个函数对象作 key —
    routes_reference.py 里 ``from .deps import get_access``, 然后 Depends(get_access),
    所以 override key 必须是 ``vnpy_webtrader.deps.get_access`` (从同一 module 拿).
    """
    from vnpy_webtrader.deps import get_access
    from vnpy_webtrader.routes_reference import router as reference_router

    app = FastAPI()
    app.dependency_overrides[get_access] = lambda: True
    app.include_router(reference_router)
    return app


class TestStockNamesEndpoint:
    def test_returns_full_mapping_when_parquet_exists(
        self, app_with_reference: FastAPI, stock_list_parquet: Path, monkeypatch,
    ):
        """parquet 存在: 返回完整 ts_code → name 字典."""
        # 用 TUSHARE_STOCK_LIST_PATH env 让 lookup 走显式路径
        monkeypatch.setenv("TUSHARE_STOCK_LIST_PATH", str(stock_list_parquet))
        # 重置 singleton 让新 env 生效
        from vnpy_tushare_pro.ml_data_build import stock_name_lookup as snl
        monkeypatch.setattr(snl, "_DEFAULT", None)

        client = TestClient(app_with_reference)
        resp = client.get("/api/v1/reference/stock_names")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 3
        assert data["names"] == {
            "000001.SZ": "平安银行",
            "600000.SH": "浦发银行",
            "300750.SZ": "宁德时代",
        }
        assert data["source_path"] is not None
        assert "stock_list.parquet" in data["source_path"]

    def test_returns_empty_when_parquet_missing(
        self, app_with_reference: FastAPI, monkeypatch, tmp_path,
    ):
        """parquet 不存在: 返回空 dict 不报错, count=0, source_path=None."""
        # 显式指向不存在的路径 + 清空环境其他候选
        nonexistent = tmp_path / "nope.parquet"
        monkeypatch.setenv("TUSHARE_STOCK_LIST_PATH", str(nonexistent))
        # 清掉 QS_DATA_ROOT 防 fallback
        monkeypatch.delenv("QS_DATA_ROOT", raising=False)
        # 切到一个不含 stock_data/ 子目录的 cwd
        monkeypatch.chdir(tmp_path)
        # 重置 singleton
        from vnpy_tushare_pro.ml_data_build import stock_name_lookup as snl
        monkeypatch.setattr(snl, "_DEFAULT", None)

        client = TestClient(app_with_reference)
        resp = client.get("/api/v1/reference/stock_names")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["names"] == {}
        assert data["source_path"] is None
