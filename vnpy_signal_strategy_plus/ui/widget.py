from typing import Dict, List, Any
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtCore, QtGui, QtWidgets
from vnpy.trader.ui.widget import (
    BaseCell,
    EnumCell,
    MsgCell,
    TimeCell,
    BaseMonitor
)
from ..engine import APP_NAME, EVENT_SIGNAL_STRATEGY_PLUS, SignalEnginePlus
from ..locale import _


class SignalStrategyWidgetPlus(QtWidgets.QWidget):
    """"""

    signal_log: QtCore.Signal = QtCore.Signal(Event)
    signal_strategy: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.signal_engine: SignalEnginePlus = main_engine.get_engine(APP_NAME)

        self.managers: Dict[str, "SignalStrategyManagerPlus"] = {}

        self.init_ui()
        self.register_event()
        self.signal_engine.init_engine()
        self.update_class_combo()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("信号策略Plus"))

        # 获取屏幕尺寸
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # 设置为屏幕的80%，最小800x600
        width = max(int(screen_width * 0.3), 1500)
        height = max(int(screen_height * 0.3), 1000)
        self.resize(width, height)

        # Create widgets
        self.class_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()

        add_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("添加策略"))
        add_button.clicked.connect(self.add_strategy)

        init_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("全部初始化"))
        init_button.clicked.connect(self.init_all_strategies)

        start_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("全部启动"))
        start_button.clicked.connect(self.signal_engine.start_all_strategies)

        stop_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("全部停止"))
        stop_button.clicked.connect(self.signal_engine.stop_all_strategies)

        clear_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("清空日志"))
        clear_button.clicked.connect(self.clear_log)

        self.scroll_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        self.scroll_layout.addStretch()

        scroll_widget: QtWidgets.QWidget = QtWidgets.QWidget()
        scroll_widget.setLayout(self.scroll_layout)

        self.scroll_area: QtWidgets.QScrollArea = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(scroll_widget)

        self.log_monitor: LogMonitorPlus = LogMonitorPlus(self.main_engine, self.event_engine)

        self.strategy_combo = QtWidgets.QComboBox()
        self.strategy_combo.setMinimumWidth(200)
        find_button = QtWidgets.QPushButton(_("查找"))
        find_button.clicked.connect(self.find_strategy)

        # Set layout
        hbox1: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox1.addWidget(self.class_combo)
        hbox1.addWidget(add_button)
        hbox1.addStretch()
        hbox1.addWidget(self.strategy_combo)
        hbox1.addWidget(find_button)
        hbox1.addStretch()
        hbox1.addWidget(init_button)
        hbox1.addWidget(start_button)
        hbox1.addWidget(stop_button)
        hbox1.addWidget(clear_button)

        grid: QtWidgets.QGridLayout = QtWidgets.QGridLayout()
        grid.addWidget(self.scroll_area, 0, 0, 2, 1)
        grid.addWidget(self.log_monitor, 0, 1, 2, 1)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(grid)

        self.setLayout(vbox)

    def init_all_strategies(self) -> None:
        """"""
        if not self.signal_engine.init_all_strategies():
            QtWidgets.QMessageBox.warning(
                self,
                "警告",
                "操作失败，请连接QMT_SIM!"
            )

    def update_class_combo(self) -> None:
        """"""
        names = self.signal_engine.get_all_strategy_class_names()
        names.sort()
        self.class_combo.addItems(names)

    def update_strategy_combo(self) -> None:
        """"""
        names = list(self.managers.keys())
        names.sort()

        self.strategy_combo.clear()
        self.strategy_combo.addItems(names)

    def register_event(self) -> None:
        """"""
        self.signal_strategy.connect(self.process_strategy_event)

        self.event_engine.register(
            EVENT_SIGNAL_STRATEGY_PLUS, self.signal_strategy.emit
        )

    def process_strategy_event(self, event: Event) -> None:
        """
        Update strategy status onto its monitor.
        """
        data = event.data
        strategy_name: str = data["strategy_name"]
        data['parameters']["db_password"] = "**************"

        if strategy_name in self.managers:
            manager: SignalStrategyManagerPlus = self.managers[strategy_name]
            manager.update_data(data)
        else:
            manager = SignalStrategyManagerPlus(self, self.signal_engine, data)
            self.scroll_layout.insertWidget(0, manager)
            self.managers[strategy_name] = manager

            self.update_strategy_combo()

    def remove_strategy(self, strategy_name: str) -> None:
        """"""
        manager: SignalStrategyManagerPlus = self.managers.pop(strategy_name)
        manager.deleteLater()

        self.update_strategy_combo()

    def add_strategy(self) -> None:
        """"""
        class_name: str = str(self.class_combo.currentText())
        if not class_name:
            return

        self.signal_engine.add_strategy(class_name)

    def find_strategy(self) -> None:
        """"""
        strategy_name = self.strategy_combo.currentText()
        if strategy_name:
            manager = self.managers[strategy_name]
            self.scroll_area.ensureWidgetVisible(manager)

    def clear_log(self) -> None:
        """"""
        self.log_monitor.setRowCount(0)

    def show(self) -> None:
        """"""
        self.showNormal()


class SignalStrategyManagerPlus(QtWidgets.QFrame):
    """
    Manager for a strategy
    """

    def __init__(
        self, signal_manager: SignalStrategyWidgetPlus, signal_engine: SignalEnginePlus, data: dict
    ) -> None:
        """"""
        super().__init__()

        self.signal_manager: SignalStrategyWidgetPlus = signal_manager
        self.signal_engine: SignalEnginePlus = signal_engine

        self.strategy_name: str = data["strategy_name"]
        self._data: dict = data

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setFixedHeight(230)
        self.setFrameShape(self.Shape.Box)
        self.setLineWidth(1)

        self.init_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("初始化"))
        self.init_button.clicked.connect(self.init_strategy)

        self.start_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("启动"))
        self.start_button.clicked.connect(self.start_strategy)
        self.start_button.setEnabled(False)

        self.stop_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("停止"))
        self.stop_button.clicked.connect(self.stop_strategy)
        self.stop_button.setEnabled(False)

        self.remove_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("移除"))
        self.remove_button.clicked.connect(self.remove_strategy)

        self.auto_test_button: QtWidgets.QPushButton | None = None
        self.clear_position_button: QtWidgets.QPushButton | None = None
        strategy = self.signal_engine.strategies.get(self.strategy_name)
        if strategy and getattr(strategy, "is_live_test_strategy", False) and hasattr(strategy, "run_live_test_suite"):
            self.auto_test_button = QtWidgets.QPushButton(_("自动化测试"))
            self.auto_test_button.clicked.connect(self.run_auto_test)
            
        if strategy and getattr(strategy, "support_clear_position", False) and hasattr(strategy, "clear_all_positions"):
            self.clear_position_button = QtWidgets.QPushButton(_("一键清仓"))
            self.clear_position_button.clicked.connect(self.run_clear_position)

        strategy_name: str = self._data["strategy_name"]
        class_name: str = self._data["class_name"]
        gateway = ""
        if strategy_name in self.signal_engine.strategies:
            gateway: str = self.signal_engine.strategies[strategy_name].gateway
        
        label_text: str = (
            f"{strategy_name}  ({class_name} - [{gateway}])"
        )
        label: QtWidgets.QLabel = QtWidgets.QLabel(label_text)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.parameters_monitor: DataMonitorPlus = DataMonitorPlus(self._data["parameters"])
        self.variables_monitor: DataMonitorPlus = DataMonitorPlus(self._data["variables"])

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.init_button)
        hbox.addWidget(self.start_button)
        hbox.addWidget(self.stop_button)
        hbox.addWidget(self.remove_button)
        if self.auto_test_button:
            hbox.addWidget(self.auto_test_button)
        if self.clear_position_button:
            hbox.addWidget(self.clear_position_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(label)
        vbox.addLayout(hbox)
        vbox.addWidget(self.parameters_monitor)
        vbox.addWidget(self.variables_monitor)
        self.setLayout(vbox)

    def run_auto_test(self) -> None:
        strategy = self.signal_engine.strategies.get(self.strategy_name)
        if not strategy:
            return
        if not getattr(strategy, "is_live_test_strategy", False):
            return

        text = (
            "<div style='font-size:14px;'>"
            "<div style='color:#b00000;font-size:16px;font-weight:700;font-style:italic;'>"
            "风险提示：即将向数据库写入测试信号，并触发真实下单流程。请务必使用模拟账户/小资金，勿在实盘账户上执行。"
            "</div>"
            "<div style='margin-top:10px;'>确认继续执行自动化测试？</div>"
            "</div>"
        )

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("自动化测试确认")
        box.setTextFormat(QtCore.Qt.TextFormat.RichText)
        box.setText(text)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        items = ["全部", "冒烟", "基础", "全量"]
        suite, ok = QtWidgets.QInputDialog.getItem(self, "选择测试套件", "套件：", items, 0, False)
        if not ok:
            return

        self.signal_engine.call_strategy_func(strategy, strategy.run_live_test_suite, suite)

    def run_clear_position(self) -> None:
        strategy = self.signal_engine.strategies.get(self.strategy_name)
        if not strategy:
            return
        if not getattr(strategy, "support_clear_position", False) or not hasattr(strategy, "clear_all_positions"):
            return

        text = (
            "<div style='font-size:14px;'>"
            "<div style='color:#b00000;font-size:16px;font-weight:700;font-style:italic;'>"
            "风险提示：即将以市价或跌停价清仓当前所有可用持仓，请确认是否继续！"
            "</div>"
            "<div style='margin-top:10px;'>确认执行一键清仓？</div>"
            "</div>"
        )

        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(_("清仓确认"))
        box.setTextFormat(QtCore.Qt.TextFormat.RichText)
        box.setText(text)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
        if box.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.signal_engine.call_strategy_func(strategy, strategy.clear_all_positions)

    def update_data(self, data: dict) -> None:
        """"""
        self._data = data

        self.parameters_monitor.update_data(data["parameters"])
        self.variables_monitor.update_data(data["variables"])

        # Update button status
        inited: bool = data["inited"]
        trading: bool = data["trading"]

        if not inited:
            return
        self.init_button.setEnabled(False)

        if trading:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.remove_button.setEnabled(False)
        else:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.remove_button.setEnabled(True)

    def init_strategy(self) -> None:
        """"""
        if not self.signal_engine.init_strategy(self.strategy_name):
            QtWidgets.QMessageBox.warning(
                self.signal_manager,
                "警告",
                "操作失败，请连接QMT_SIM!"
            )
            return

    def start_strategy(self) -> None:
        """"""
        self.signal_engine.start_strategy(self.strategy_name)

    def stop_strategy(self) -> None:
        """"""
        self.signal_engine.stop_strategy(self.strategy_name)

    def remove_strategy(self) -> None:
        """"""
        self.signal_engine.remove_strategy(self.strategy_name)
        self.signal_manager.remove_strategy(self.strategy_name)


class DataMonitorPlus(QtWidgets.QTableWidget):
    """
    Table monitor for parameters and variables.
    """

    def __init__(self, data: dict) -> None:
        """"""
        super().__init__()

        self._data: dict = data
        self.cells: dict = {}

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        labels: list = list(self._data.keys())
        self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)

        self.setRowCount(1)
        self.verticalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)

        for column, name in enumerate(self._data.keys()):
            value = self._data[name]

            cell: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(str(value))
            cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

            self.setItem(0, column, cell)
            self.cells[name] = cell

    def update_data(self, data: dict) -> None:
        """"""
        for name, value in data.items():
            if name in self.cells:
                cell: QtWidgets.QTableWidgetItem = self.cells[name]
                if name == "db_password":
                    value = "********"
                    continue
                cell.setText(str(value))


class LogMonitorPlus(BaseMonitor):
    """
    Monitor for log data.
    """

    event_type: str = "eLog" # EVENT_LOG
    data_key: str = ""
    sorting: bool = False

    headers: dict = {
        "time": {"display": _("时间"), "cell": TimeCell, "update": False},
        "msg": {"display": _("信息"), "cell": MsgCell, "update": False},
    }

    def init_ui(self) -> None:
        """
        Stretch last column.
        """
        super().init_ui()

        self.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )

    def insert_new_row(self, data: Any) -> None:
        """
        Insert a new row at the top of table.
        """
        # Filter log by gateway_name
        if data.gateway_name != APP_NAME:
            return

        super().insert_new_row(data)
        self.resizeRowToContents(0)
