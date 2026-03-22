"""
Implements main window of the trading platform.
"""

from types import ModuleType
import webbrowser
from functools import partial
from importlib import import_module
from typing import TypeVar
from collections.abc import Callable
from pathlib import Path

import vnpy
from vnpy.event import EventEngine

from .qt import QtCore, QtGui, QtWidgets
from .dockhost import DockHost, DockWidgetHandle, create_dock_host
from .widget import (
    BaseMonitor,
    TickMonitor,
    OrderMonitor,
    TradeMonitor,
    PositionMonitor,
    AccountMonitor,
    LogMonitor,
    ActiveOrderMonitor,
    GatewayConnectionStatusButton,
    ConnectDialog,
    ContractManager,
    TradingWidget,
    AboutDialog,
    GlobalDialog
)
from ..engine import MainEngine, BaseApp
from ..utility import get_icon_path, TRADER_DIR
from ..locale import _


WidgetType = TypeVar("WidgetType", bound="QtWidgets.QWidget")


class MainWindow(QtWidgets.QMainWindow):
    """
    Main window of the trading platform.
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.window_title: str = _("VeighNa Trader 社区版 - {}   [{}]").format(vnpy.__version__, TRADER_DIR)

        self.widgets: dict[str, QtWidgets.QWidget] = {}
        self.monitors: dict[str, BaseMonitor] = {}
        self.dock_host: DockHost | None = None

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(self.window_title)
        self.init_dock()
        self.init_toolbar()
        self.init_menu()
        self.init_status_bar()
        self.load_window_setting("custom")

    def init_status_bar(self) -> None:
        """"""
        status_bar: QtWidgets.QStatusBar = QtWidgets.QStatusBar()
        self.setStatusBar(status_bar)

        gateway_status: GatewayConnectionStatusButton = GatewayConnectionStatusButton(self.main_engine, self)
        status_bar.addPermanentWidget(gateway_status)

    def init_dock(self) -> None:
        """"""
        self.dock_host = create_dock_host(self)

        self.trading_widget, trading_dock = self.create_dock(
            TradingWidget, _("交易"), QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
        )
        tick_widget, tick_dock = self.create_dock(
            TickMonitor, _("行情"), QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        account_widget, order_dock = self.create_dock(
            AccountMonitor, _("资金"), QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        active_widget, active_dock = self.create_dock(
            ActiveOrderMonitor, _("活动"), QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        position_widget, trade_dock = self.create_dock(
            PositionMonitor, _("持仓"), QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        log_widget, log_dock = self.create_dock(
            LogMonitor, _("日志"), QtCore.Qt.DockWidgetArea.BottomDockWidgetArea
        )
        order_widget, account_dock = self.create_dock(
            OrderMonitor, _("委托"), QtCore.Qt.DockWidgetArea.BottomDockWidgetArea
        )
        trade_widget, position_dock = self.create_dock(
            TradeMonitor, _("成交"), QtCore.Qt.DockWidgetArea.BottomDockWidgetArea
        )

        if self.dock_host:
            self.dock_host.tabify(active_dock, order_dock)

        self.save_window_setting("default")

        tick_widget.itemDoubleClicked.connect(self.trading_widget.update_with_cell)
        position_widget.itemDoubleClicked.connect(self.trading_widget.update_with_cell)

    def init_menu(self) -> None:
        """"""
        bar: QtWidgets.QMenuBar = self.menuBar()
        bar.setNativeMenuBar(False)     # for mac and linux

        # System menu
        sys_menu: QtWidgets.QMenu = bar.addMenu(_("系统"))

        gateway_names: list = self.main_engine.get_all_gateway_names()
        for name in gateway_names:
            func: Callable = partial(self.connect_gateway, name)
            self.add_action(
                sys_menu,
                _("连接{}").format(name),
                get_icon_path(__file__, "connect.ico"),
                func
            )

        sys_menu.addSeparator()

        self.add_action(
            sys_menu,
            _("退出"),
            get_icon_path(__file__, "exit.ico"),
            self.close
        )

        # App menu
        app_menu: QtWidgets.QMenu = bar.addMenu(_("功能"))

        all_apps: list[BaseApp] = self.main_engine.get_all_apps()
        for app in all_apps:
            ui_module: ModuleType = import_module(app.app_module + ".ui")
            widget_class: type[QtWidgets.QWidget] = getattr(ui_module, app.widget_name)

            func = partial(self.open_widget, widget_class, app.app_name)

            self.add_action(app_menu, app.display_name, app.icon_name, func, True)

        # Window menu
        self.window_menu: QtWidgets.QMenu = bar.addMenu(_("窗口"))
        self.window_menu.aboutToShow.connect(self._refresh_window_menu)

        # Global setting editor
        action: QtGui.QAction = QtGui.QAction(_("配置"), self)
        action.triggered.connect(self.edit_global_setting)
        bar.addAction(action)

        # Help menu
        help_menu: QtWidgets.QMenu = bar.addMenu(_("帮助"))

        self.add_action(
            help_menu,
            _("查询合约"),
            get_icon_path(__file__, "contract.ico"),
            partial(self.open_widget, ContractManager, "contract"),
            True
        )

        self.add_action(
            help_menu,
            _("还原窗口"),
            get_icon_path(__file__, "restore.ico"),
            self.restore_window_setting
        )

        self.add_action(
            help_menu,
            _("测试邮件"),
            get_icon_path(__file__, "email.ico"),
            self.send_test_email
        )

        self.add_action(
            help_menu,
            _("社区论坛"),
            get_icon_path(__file__, "forum.ico"),
            self.open_forum,
            True
        )

        self.add_action(
            help_menu,
            _("关于"),
            get_icon_path(__file__, "about.ico"),
            partial(self.open_widget, AboutDialog, "about"),
        )

    def init_toolbar(self) -> None:
        """"""
        self.toolbar: QtWidgets.QToolBar = QtWidgets.QToolBar(self)
        self.toolbar.setObjectName(_("工具栏"))
        self.toolbar.setFloatable(False)
        self.toolbar.setMovable(False)

        # Set button size
        w: int = 40
        size = QtCore.QSize(w, w)
        self.toolbar.setIconSize(size)

        # Set button spacing
        layout: QtWidgets.QLayout | None = self.toolbar.layout()
        if layout:
            layout.setSpacing(10)

        self.addToolBar(QtCore.Qt.ToolBarArea.LeftToolBarArea, self.toolbar)

    def add_action(
        self,
        menu: QtWidgets.QMenu,
        action_name: str,
        icon_name: str,
        func: Callable,
        toolbar: bool = False
    ) -> None:
        """"""
        icon: QtGui.QIcon = QtGui.QIcon(icon_name)

        action: QtGui.QAction = QtGui.QAction(action_name, self)
        action.triggered.connect(func)
        action.setIcon(icon)

        menu.addAction(action)

        if toolbar:
            self.toolbar.addAction(action)

    def create_dock(
        self,
        widget_class: type[WidgetType],
        name: str,
        area: QtCore.Qt.DockWidgetArea
    ) -> tuple[WidgetType, DockWidgetHandle]:
        """
        Initialize a dock widget.
        """
        widget: WidgetType = widget_class(self.main_engine, self.event_engine)      # type: ignore
        if isinstance(widget, BaseMonitor):
            self.monitors[name] = widget

        if not self.dock_host:
            self.dock_host = create_dock_host(self)

        dock: DockWidgetHandle = self.dock_host.create_dock(widget, name, area)
        return widget, dock

    def connect_gateway(self, gateway_name: str) -> None:
        """
        Open connect dialog for gateway connection.
        """
        dialog: ConnectDialog = ConnectDialog(self.main_engine, gateway_name)
        dialog.exec()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """
        Call main engine close function before exit.
        """
        reply = QtWidgets.QMessageBox.question(
            self,
            _("退出"),
            _("确认退出？"),
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            for widget in self.widgets.values():
                widget.close()

            for monitor in self.monitors.values():
                monitor.save_setting()

            self.save_window_setting("custom")

            self.main_engine.close()

            event.accept()
        else:
            event.ignore()

    def open_widget(self, widget_class: type[QtWidgets.QWidget], name: str) -> None:
        """
        Open contract manager.
        """
        widget: QtWidgets.QWidget | None = self.widgets.get(name, None)
        if not widget:
            widget = widget_class(self.main_engine, self.event_engine)      # type: ignore
            self.widgets[name] = widget

        if isinstance(widget, QtWidgets.QDialog):
            widget.exec()
        else:
            widget.show()

    def save_window_setting(self, name: str) -> None:
        """
        Save current window size and state to config/myapp.ini file.
        """
        # 确保config目录存在
        config_dir = Path.cwd() / "config"
        config_dir.mkdir(exist_ok=True)
        
        # 使用统一的配置文件
        config_file = config_dir / "myapp.ini"
        settings: QtCore.QSettings = QtCore.QSettings(str(config_file), QtCore.QSettings.Format.IniFormat)
        
        state_key: str = self._get_state_key()
        if self.dock_host:
            settings.setValue(f"{name}/{state_key}", self.dock_host.save_state())
        else:
            settings.setValue(f"{name}/{state_key}", self.saveState())
        settings.setValue(f"{name}/geometry", self.saveGeometry())
        
        # 保存dock可见性信息
        if self.dock_host:
            dock_visibility = {}
            for dock in self.dock_host.iter_docks():
                title = self.dock_host.get_dock_title(dock)
                if title:
                    dock_visibility[title] = self.dock_host.is_dock_visible(dock)
            
            # 将可见性信息转换为JSON字符串保存
            import json
            settings.setValue(f"{name}/dock_visibility", json.dumps(dock_visibility, ensure_ascii=False))

    def load_window_setting(self, name: str, *, log_restore_failure: bool = True) -> None:
        """
        Load previous window size and state from config/myapp.ini file.
        """
        # 使用统一的配置文件
        config_file = Path.cwd() / "config" / "myapp.ini"
        if not config_file.exists():
            return
            
        settings: QtCore.QSettings = QtCore.QSettings(str(config_file), QtCore.QSettings.Format.IniFormat)
        state_key: str = self._get_state_key()
        state = settings.value(f"{name}/{state_key}")
        backend: str = getattr(self.dock_host, "BACKEND", "qt") if self.dock_host else "qt"
        if state is None and backend == "qt":
            state = settings.value(f"{name}/state")
        geometry = settings.value(f"{name}/geometry")

        if isinstance(state, QtCore.QByteArray) and not state.isEmpty():
            restored: bool = False
            try:
                if self.dock_host:
                    restored = self.dock_host.restore_state(state)
                else:
                    restored = bool(self.restoreState(state))
            except Exception:
                restored = False

            if not restored:
                if log_restore_failure:
                    backend: str = getattr(self.dock_host, "BACKEND", "qt") if self.dock_host else "qt"
                    if name != "default":
                        self.main_engine.write_log(f"窗口布局恢复失败（{name}/{backend}），已回退到默认布局")
                    else:
                        self.main_engine.write_log(f"默认窗口布局恢复失败（{backend}）")
                try:
                    settings.remove(state_key)
                except Exception:
                    pass
                if name != "default":
                    self.load_window_setting("default", log_restore_failure=False)
                return

        if isinstance(geometry, QtCore.QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
            
        # 读取并应用dock可见性设置
        if self.dock_host:
            dock_visibility_json = settings.value(f"{name}/dock_visibility")
            if dock_visibility_json:
                try:
                    import json
                    dock_visibility = json.loads(dock_visibility_json)
                    
                    # 应用可见性设置
                    for dock in self.dock_host.iter_docks():
                        title = self.dock_host.get_dock_title(dock)
                        if title and title in dock_visibility:
                            self.dock_host.set_dock_visible(dock, dock_visibility[title])
                except (json.JSONDecodeError, Exception):
                    # JSON解析失败时忽略，保持默认可见性
                    pass

    def _get_state_key(self) -> str:
        backend: str = getattr(self.dock_host, "BACKEND", "qt") if self.dock_host else "qt"
        return f"state_{backend}"

    def restore_window_setting(self) -> None:
        """
        Restore window to default setting.
        """
        self.load_window_setting("default")
        # self.showMaximized()

    def send_test_email(self) -> None:
        """
        Sending a test email.
        """
        self.main_engine.send_email("VeighNa Trader", "testing", None)

    def open_forum(self) -> None:
        """
        """
        webbrowser.open("https://www.vnpy.com/forum/")

    def edit_global_setting(self) -> None:
        """
        """
        dialog: GlobalDialog = GlobalDialog()
        dialog.exec()

    def _refresh_window_menu(self) -> None:
        """
        Refresh window menu with current dock widgets.
        """
        self.window_menu.clear()
        
        if not self.dock_host:
            return
            
        # Add menu items for each dock
        for dock in self.dock_host.iter_docks():
            title: str = self.dock_host.get_dock_title(dock)
            if not title:
                continue
                
            is_visible: bool = self.dock_host.is_dock_visible(dock)
            
            action: QtGui.QAction = QtGui.QAction(title, self)
            action.setCheckable(True)
            action.setChecked(is_visible)
            action.triggered.connect(lambda checked, d=dock: self._toggle_dock(d, checked))
            
            self.window_menu.addAction(action)

    def _toggle_dock(self, dock: DockWidgetHandle, visible: bool) -> None:
        """
        Toggle dock widget visibility.
        """
        if self.dock_host:
            self.dock_host.set_dock_visible(dock, visible)
