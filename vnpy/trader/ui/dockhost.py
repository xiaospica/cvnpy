from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from typing import Any, Final

from .qt import QtCore, QtWidgets


DockWidgetHandle = Any


def _get_dock_backend() -> str:
    return os.getenv("VNPY_DOCK_BACKEND", "").strip().lower()


def _try_import_qtads() -> Any | None:
    try:
        pkg = importlib.import_module("PySide6QtAds")
    except Exception:
        import traceback
        print(traceback.format_exc())
        return None

    # PySide6-QtAds 4.3.1.4: CDockManager 等符号直接暴露在 PySide6QtAds 顶层模块上，
    # 不再提供 QtAds 子模块/属性。优先兼容该版本。
    if hasattr(pkg, "CDockManager"):
        return pkg

    qtads = getattr(pkg, "QtAds", None)
    if qtads is not None:
        return qtads

    try:
        mod = importlib.import_module("PySide6QtAds.QtAds")
        return mod
    except Exception:
        return None


class DockHost(ABC):
    def __init__(self, main_window: QtWidgets.QMainWindow) -> None:
        self.main_window: QtWidgets.QMainWindow = main_window

    @abstractmethod
    def create_dock(
        self,
        widget: QtWidgets.QWidget,
        title: str,
        area: QtCore.Qt.DockWidgetArea,
    ) -> DockWidgetHandle:
        raise NotImplementedError

    @abstractmethod
    def tabify(self, first: DockWidgetHandle, second: DockWidgetHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_state(self) -> QtCore.QByteArray:
        raise NotImplementedError

    @abstractmethod
    def restore_state(self, state: QtCore.QByteArray) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_dock_title(self, dock: DockWidgetHandle) -> str:
        """获取dock的标题"""
        raise NotImplementedError

    @abstractmethod
    def set_dock_visible(self, dock: DockWidgetHandle, visible: bool) -> None:
        """设置dock的可见性"""
        raise NotImplementedError

    @abstractmethod
    def is_dock_visible(self, dock: DockWidgetHandle) -> bool:
        """判断dock是否可见"""
        raise NotImplementedError

    @abstractmethod
    def iter_docks(self):
        """迭代所有dock"""
        raise NotImplementedError


class QtDockHost(DockHost):
    BACKEND: Final[str] = "qt"

    FEATURES: Final = (
        QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
    )

    def create_dock(
        self,
        widget: QtWidgets.QWidget,
        title: str,
        area: QtCore.Qt.DockWidgetArea,
    ) -> QtWidgets.QDockWidget:
        dock: QtWidgets.QDockWidget = QtWidgets.QDockWidget(title)
        dock.setWidget(widget)
        dock.setObjectName(title)
        dock.setFeatures(self.FEATURES)
        self.main_window.addDockWidget(area, dock)
        return dock

    def tabify(self, first: QtWidgets.QDockWidget, second: QtWidgets.QDockWidget) -> None:
        self.main_window.tabifyDockWidget(first, second)

    def save_state(self) -> QtCore.QByteArray:
        return self.main_window.saveState()

    def restore_state(self, state: QtCore.QByteArray) -> bool:
        return bool(self.main_window.restoreState(state))

    def get_dock_title(self, dock: QtWidgets.QDockWidget) -> str:
        """获取dock的标题"""
        return dock.windowTitle()

    def set_dock_visible(self, dock: QtWidgets.QDockWidget, visible: bool) -> None:
        """设置dock的可见性"""
        dock.setVisible(visible)

    def is_dock_visible(self, dock: QtWidgets.QDockWidget) -> bool:
        """判断dock是否可见"""
        return dock.isVisible()

    def iter_docks(self):
        """迭代所有dock"""
        for dock in self.main_window.findChildren(QtWidgets.QDockWidget):
            yield dock


class AdsDockHost(DockHost):
    BACKEND: Final[str] = "ads"

    def __init__(self, main_window: QtWidgets.QMainWindow) -> None:
        super().__init__(main_window)

        qtads = _try_import_qtads()
        if qtads is None:
            raise RuntimeError("PySide6-QtAds 未安装或无法导入")

        self._qtads: Any = qtads
        self._qtads.CDockManager.setConfigFlag(
            self._qtads.CDockManager.FocusHighlighting, 
            True
        )
        self.dock_manager: Any = self._qtads.CDockManager(main_window)
        main_window.setCentralWidget(self.dock_manager)

    @classmethod
    def is_available(cls) -> bool:
        qtads = _try_import_qtads()
        return bool(qtads and hasattr(qtads, "CDockManager"))

    def _map_area(self, area: QtCore.Qt.DockWidgetArea) -> Any:
        ads_area = getattr(self._qtads, "DockWidgetArea", None)
        if ads_area is None:
            ads_area = self._qtads

        if area == QtCore.Qt.DockWidgetArea.LeftDockWidgetArea:
            return ads_area.LeftDockWidgetArea
        if area == QtCore.Qt.DockWidgetArea.RightDockWidgetArea:
            return ads_area.RightDockWidgetArea
        if area == QtCore.Qt.DockWidgetArea.TopDockWidgetArea:
            return ads_area.TopDockWidgetArea
        if area == QtCore.Qt.DockWidgetArea.BottomDockWidgetArea:
            return ads_area.BottomDockWidgetArea
        return ads_area.LeftDockWidgetArea

    def create_dock(
        self,
        widget: QtWidgets.QWidget,
        title: str,
        area: QtCore.Qt.DockWidgetArea,
    ) -> Any:
        dock: Any = self._qtads.CDockWidget(title)
        dock.setWidget(widget)
        set_object_name = getattr(dock, "setObjectName", None)
        if callable(set_object_name):
            set_object_name(title)

        ads_area = self._map_area(area)
        self.dock_manager.addDockWidget(ads_area, dock)
        return dock

    def tabify(self, first: Any, second: Any) -> None:
        ads_area = self._map_area(QtCore.Qt.DockWidgetArea.RightDockWidgetArea)

        add_tab_to_area = getattr(self.dock_manager, "addDockWidgetTabToArea", None)
        if callable(add_tab_to_area):
            area_widget = getattr(first, "dockAreaWidget", None)
            if callable(area_widget):
                add_tab_to_area(second, area_widget())
                return

        add_tab = getattr(self.dock_manager, "addDockWidgetTab", None)
        if callable(add_tab):
            add_tab(ads_area, second)
            return

        add_dock = getattr(self.dock_manager, "addDockWidget", None)
        if callable(add_dock):
            try:
                add_dock(ads_area, second, first)
                return
            except Exception:
                add_dock(ads_area, second)

    def save_state(self) -> QtCore.QByteArray:
        save = getattr(self.dock_manager, "saveState", None)
        if callable(save):
            return save()
        return QtCore.QByteArray()

    def restore_state(self, state: QtCore.QByteArray) -> bool:
        restore = getattr(self.dock_manager, "restoreState", None)
        if callable(restore):
            try:
                return bool(restore(state))
            except Exception:
                return False
        return False

    def get_dock_title(self, dock: Any) -> str:
        """获取dock的标题"""
        title = getattr(dock, "windowTitle", None)
        if callable(title):
            return title()
        return ""

    def set_dock_visible(self, dock: Any, visible: bool) -> None:
        """设置dock的可见性"""
        set_visible = getattr(dock, "setVisible", None)
        if callable(set_visible):
            set_visible(visible)

    def is_dock_visible(self, dock: Any) -> bool:
        """判断dock是否可见"""
        is_visible = getattr(dock, "isVisible", None)
        if callable(is_visible):
            return is_visible()
        return False

    def iter_docks(self):
        """迭代所有dock"""
        dock_widgets = getattr(self.dock_manager, "dockWidgets", None)
        if callable(dock_widgets):
            for dock in dock_widgets():
                yield dock


def create_dock_host(main_window: QtWidgets.QMainWindow) -> DockHost:
    backend = _get_dock_backend()
    if backend == "ads" and AdsDockHost.is_available():
        try:
            return AdsDockHost(main_window)
        except Exception:
            return QtDockHost(main_window)
    return QtDockHost(main_window)
