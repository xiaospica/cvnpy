from __future__ import annotations

import importlib

try:
    import pytest
except ImportError:
    pytest = None


def _import_qtads_pkg() -> object | None:
    try:
        return importlib.import_module("PySide6QtAds")
    except ModuleNotFoundError:
        if pytest is not None:
            pytest.skip("PySide6-QtAds 未安装（可选依赖），跳过 QtAds 相关测试")
        return None


def test_pyside6_qtads_can_import() -> None:
    mod = _import_qtads_pkg()
    if mod is None:
        return
    assert getattr(mod, "__name__", "") == "PySide6QtAds"


def test_pyside6_qtads_qtads_api_available() -> None:
    pkg = _import_qtads_pkg()
    if pkg is None:
        return

    # PySide6-QtAds 4.3.1.4: API 直接在 PySide6QtAds 顶层模块暴露
    qtads = pkg if hasattr(pkg, "CDockManager") else getattr(pkg, "QtAds", None)

    # 旧版本：QtAds 子模块（PySide6QtAds.QtAds）
    if qtads is None or not hasattr(qtads, "CDockManager"):
        try:
            qtads = importlib.import_module("PySide6QtAds.QtAds")
        except Exception:
            qtads = None

    assert qtads is not None and hasattr(qtads, "CDockManager")
