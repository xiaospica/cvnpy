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
from ..engine import SignalEngine, APP_NAME
from ..locale import _


class SignalStrategyWidget(QtWidgets.QWidget):
    """"""

    signal_log: QtCore.Signal = QtCore.Signal(Event)
    signal_strategy: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.signal_engine: SignalEngine = main_engine.get_engine(APP_NAME)

        self.managers: Dict[str, SignalStrategyManager] = {}

        self.init_ui()
        self.register_event()
        self.signal_engine.init_engine()
        self.update_class_combo()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("信号策略"))

        # Create widgets
        self.class_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()

        add_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("添加策略"))
        add_button.clicked.connect(self.add_strategy)

        init_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("全部初始化"))
        init_button.clicked.connect(self.signal_engine.init_all_strategies)

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

        self.log_monitor: LogMonitor = LogMonitor(self.main_engine, self.event_engine)

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
            "EVENT_SIGNAL_STRATEGY", self.signal_strategy.emit
        )

    def process_strategy_event(self, event: Event) -> None:
        """
        Update strategy status onto its monitor.
        """
        data = event.data
        strategy_name: str = data["strategy_name"]

        if strategy_name in self.managers:
            manager: SignalStrategyManager = self.managers[strategy_name]
            manager.update_data(data)
        else:
            manager = SignalStrategyManager(self, self.signal_engine, data)
            self.scroll_layout.insertWidget(0, manager)
            self.managers[strategy_name] = manager

            self.update_strategy_combo()

    def remove_strategy(self, strategy_name: str) -> None:
        """"""
        manager: SignalStrategyManager = self.managers.pop(strategy_name)
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


class SignalStrategyManager(QtWidgets.QFrame):
    """
    Manager for a strategy
    """

    def __init__(
        self, signal_manager: SignalStrategyWidget, signal_engine: SignalEngine, data: dict
    ) -> None:
        """"""
        super().__init__()

        self.signal_manager: SignalStrategyWidget = signal_manager
        self.signal_engine: SignalEngine = signal_engine

        self.strategy_name: str = data["strategy_name"]
        self._data: dict = data

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setFixedHeight(300)
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

        strategy_name: str = self._data["strategy_name"]
        class_name: str = self._data["class_name"]
        author: str = self._data["author"]

        label_text: str = (
            f"{strategy_name}  ({class_name} by {author})"
        )
        label: QtWidgets.QLabel = QtWidgets.QLabel(label_text)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.parameters_monitor: DataMonitor = DataMonitor(self._data["parameters"])
        self.variables_monitor: DataMonitor = DataMonitor(self._data["variables"])

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.init_button)
        hbox.addWidget(self.start_button)
        hbox.addWidget(self.stop_button)
        hbox.addWidget(self.remove_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(label)
        vbox.addLayout(hbox)
        vbox.addWidget(self.parameters_monitor)
        vbox.addWidget(self.variables_monitor)
        self.setLayout(vbox)

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
        self.signal_engine.init_strategy(self.strategy_name)

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


class DataMonitor(QtWidgets.QTableWidget):
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
            QtWidgets.QHeaderView.ResizeMode.Stretch
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
                cell.setText(str(value))


class LogMonitor(BaseMonitor):
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
            1, QtWidgets.QHeaderView.ResizeMode.Stretch
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
