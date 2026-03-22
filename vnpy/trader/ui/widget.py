"""
Basic widgets for UI.
"""

import csv
import platform
from enum import Enum
from typing import cast, Any
from copy import copy
from tzlocal import get_localzone_name
from datetime import datetime
from importlib import metadata
from collections.abc import Iterable

from .qt import QtCore, QtGui, QtWidgets, Qt
from ..constant import Direction, Exchange, Offset, OrderType
from ..engine import MainEngine, Event, EventEngine
from ..event import (
    EVENT_QUOTE,
    EVENT_TICK,
    EVENT_TRADE,
    EVENT_ORDER,
    EVENT_POSITION,
    EVENT_ACCOUNT,
    EVENT_LOG
)
from ..object import (
    OrderRequest,
    SubscribeRequest,
    CancelRequest,
    ContractData,
    PositionData,
    OrderData,
    QuoteData,
    TickData
)
from ..utility import load_json, save_json, get_digits, ZoneInfo
from ..setting import SETTING_FILENAME, SETTINGS
from ..locale import _


COLOR_LONG = QtGui.QColor("red")
COLOR_SHORT = QtGui.QColor("green")
COLOR_BID = QtGui.QColor(255, 174, 201)
COLOR_ASK = QtGui.QColor(160, 255, 160)
COLOR_BLACK = QtGui.QColor("black")


class BaseCell(QtWidgets.QTableWidgetItem):
    """
    General cell used in tablewidgets.
    """

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__()

        self._text: str = ""
        self._data: Any = None

        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.set_content(content, data)

    def set_content(self, content: Any, data: Any) -> None:
        """
        Set text content.
        """
        self._text = str(content)
        self._data = data

        self.setText(self._text)

    def get_data(self) -> Any:
        """
        Get data object.
        """
        return self._data

    def __lt__(self, other: "BaseCell") -> bool:        # type: ignore
        """
        Sort by text content.
        """
        result: bool = self._text < other._text
        return result


class EnumCell(BaseCell):
    """
    Cell used for showing enum data.
    """

    def __init__(self, content: Enum, data: Any) -> None:
        """"""
        super().__init__(content, data)

    def set_content(self, content: Any, data: Any) -> None:
        """
        Set text using enum.constant.value.
        """
        if content:
            super().set_content(content.value, data)


class DirectionCell(EnumCell):
    """
    Cell used for showing direction data.
    """

    def __init__(self, content: Enum, data: Any) -> None:
        """"""
        super().__init__(content, data)

    def set_content(self, content: Any, data: Any) -> None:
        """
        Cell color is set according to direction.
        """
        super().set_content(content, data)

        if content is Direction.SHORT:
            self.setForeground(COLOR_SHORT)
        else:
            self.setForeground(COLOR_LONG)


class BidCell(BaseCell):
    """
    Cell used for showing bid price and volume.
    """

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__(content, data)

        self.setForeground(COLOR_BID)


class AskCell(BaseCell):
    """
    Cell used for showing ask price and volume.
    """

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__(content, data)

        self.setForeground(COLOR_ASK)


class PnlCell(BaseCell):
    """
    Cell used for showing pnl data.
    """

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__(content, data)

    def set_content(self, content: Any, data: Any) -> None:
        """
        Cell color is set based on whether pnl is
        positive or negative.
        """
        super().set_content(content, data)

        if str(content).startswith("-"):
            self.setForeground(COLOR_SHORT)
        else:
            self.setForeground(COLOR_LONG)


class TimeCell(BaseCell):
    """
    Cell used for showing time string from datetime object.
    """

    local_tz = ZoneInfo(get_localzone_name())

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__(content, data)

    def set_content(self, content: datetime | None, data: Any) -> None:
        """"""
        if content is None:
            return

        content = content.astimezone(self.local_tz)
        timestamp: str = content.strftime("%H:%M:%S")

        millisecond: int = int(content.microsecond / 1000)
        if millisecond:
            timestamp = f"{timestamp}.{millisecond}"
        else:
            timestamp = f"{timestamp}.000"

        self.setText(timestamp)
        self._data = data


class DateCell(BaseCell):
    """
    Cell used for showing date string from datetime object.
    """

    def __init__(self, content: Any, data: Any) -> None:
        """"""
        super().__init__(content, data)

    def set_content(self, content: Any, data: Any) -> None:
        """"""
        if content is None:
            return

        self.setText(content.strftime("%Y-%m-%d"))
        self._data = data


class MsgCell(BaseCell):
    """
    Cell used for showing msg data.
    """

    def __init__(self, content: str, data: Any) -> None:
        """"""
        super().__init__(content, data)
        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)


class BaseMonitor(QtWidgets.QTableWidget):
    """
    Monitor data update.
    """

    event_type: str = ""
    data_key: str = ""
    sorting: bool = False
    headers: dict = {}

    signal: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.cells: dict[str, dict] = {}

        self.init_ui()
        self.load_setting()
        self.register_event()

    def init_ui(self) -> None:
        """"""
        self.init_table()
        self.init_menu()

    def init_table(self) -> None:
        """
        Initialize table.
        """
        self.setColumnCount(len(self.headers))

        labels: list = [d["display"] for d in self.headers.values()]
        self.setHorizontalHeaderLabels(labels)

        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(self.sorting)

    def init_menu(self) -> None:
        """
        Create right click menu.
        """
        self.menu: QtWidgets.QMenu = QtWidgets.QMenu(self)

        resize_action: QtGui.QAction = QtGui.QAction(_("调整列宽"), self)
        resize_action.triggered.connect(self.resize_columns)
        self.menu.addAction(resize_action)

        save_action: QtGui.QAction = QtGui.QAction(_("保存数据"), self)
        save_action.triggered.connect(self.save_csv)
        self.menu.addAction(save_action)

    def register_event(self) -> None:
        """
        Register event handler into event engine.
        """
        if self.event_type:
            self.signal.connect(self.process_event)
            self.event_engine.register(self.event_type, self.signal.emit)

    def process_event(self, event: Event) -> None:
        """
        Process new data from event and update into table.
        """
        # Disable sorting to prevent unwanted error.
        if self.sorting:
            self.setSortingEnabled(False)

        # Update data into table.
        data = event.data

        if not self.data_key:
            self.insert_new_row(data)
        else:
            key: str = data.__getattribute__(self.data_key)

            if key in self.cells:
                self.update_old_row(data)
            else:
                self.insert_new_row(data)

        # Enable sorting
        if self.sorting:
            self.setSortingEnabled(True)

    def insert_new_row(self, data: Any) -> None:
        """
        Insert a new row at the top of table.
        """
        self.insertRow(0)

        row_cells: dict = {}
        for column, header in enumerate(self.headers.keys()):
            setting: dict = self.headers[header]

            content = data.__getattribute__(header)
            cell: QtWidgets.QTableWidgetItem = setting["cell"](content, data)
            self.setItem(0, column, cell)

            if setting["update"]:
                row_cells[header] = cell

        if self.data_key:
            key: str = data.__getattribute__(self.data_key)
            self.cells[key] = row_cells

    def update_old_row(self, data: Any) -> None:
        """
        Update an old row in table.
        """
        key: str = data.__getattribute__(self.data_key)
        row_cells = self.cells[key]

        for header, cell in row_cells.items():
            content = data.__getattribute__(header)
            cell.set_content(content, data)

    def resize_columns(self) -> None:
        """
        Resize all columns according to contents.
        """
        self.horizontalHeader().resizeSections(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

    def save_csv(self) -> None:
        """
        Save table data into a csv file
        """
        path, __ = QtWidgets.QFileDialog.getSaveFileName(
            self, _("保存数据"), "", "CSV(*.csv)")

        if not path:
            return

        with open(path, "w") as f:
            writer = csv.writer(f, lineterminator="\n")

            headers: list = [d["display"] for d in self.headers.values()]
            writer.writerow(headers)

            for row in range(self.rowCount()):
                if self.isRowHidden(row):
                    continue

                row_data: list = []
                for column in range(self.columnCount()):
                    item: QtWidgets.QTableWidgetItem | None = self.item(row, column)
                    if item:
                        row_data.append(str(item.text()))
                    else:
                        row_data.append("")
                writer.writerow(row_data)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        """
        Show menu with right click.
        """
        self.menu.popup(QtGui.QCursor.pos())

    def save_setting(self) -> None:
        """"""
        settings: QtCore.QSettings = QtCore.QSettings(self.__class__.__name__, "custom")
        settings.setValue("column_state", self.horizontalHeader().saveState())

    def load_setting(self) -> None:
        """"""
        settings: QtCore.QSettings = QtCore.QSettings(self.__class__.__name__, "custom")
        column_state = settings.value("column_state")

        if isinstance(column_state, QtCore.QByteArray):
            self.horizontalHeader().restoreState(column_state)
            self.horizontalHeader().setSortIndicator(-1, QtCore.Qt.SortOrder.AscendingOrder)


class TickMonitor(BaseMonitor):
    """
    Monitor for tick data.
    """

    event_type: str = EVENT_TICK
    data_key: str = "vt_symbol"
    sorting: bool = True

    headers: dict = {
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "name": {"display": _("名称"), "cell": BaseCell, "update": True},
        "last_price": {"display": _("最新价"), "cell": BaseCell, "update": True},
        "volume": {"display": _("成交量"), "cell": BaseCell, "update": True},
        "open_price": {"display": _("开盘价"), "cell": BaseCell, "update": True},
        "high_price": {"display": _("最高价"), "cell": BaseCell, "update": True},
        "low_price": {"display": _("最低价"), "cell": BaseCell, "update": True},
        "bid_price_1": {"display": _("买1价"), "cell": BidCell, "update": True},
        "bid_volume_1": {"display": _("买1量"), "cell": BidCell, "update": True},
        "ask_price_1": {"display": _("卖1价"), "cell": AskCell, "update": True},
        "ask_volume_1": {"display": _("卖1量"), "cell": AskCell, "update": True},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class LogMonitor(BaseMonitor):
    """
    Monitor for log data.
    """

    event_type: str = EVENT_LOG
    data_key: str = ""
    sorting: bool = False

    headers: dict = {
        "time": {"display": _("时间"), "cell": TimeCell, "update": False},
        "msg": {"display": _("信息"), "cell": MsgCell, "update": False},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class TradeMonitor(BaseMonitor):
    """
    Monitor for trade data.
    """

    event_type: str = EVENT_TRADE
    data_key: str = ""
    sorting: bool = True

    headers: dict = {
        "tradeid": {"display": _("成交号"), "cell": BaseCell, "update": False},
        "orderid": {"display": _("委托号"), "cell": BaseCell, "update": False},
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "offset": {"display": _("开平"), "cell": EnumCell, "update": False},
        "price": {"display": _("价格"), "cell": BaseCell, "update": False},
        "volume": {"display": _("数量"), "cell": BaseCell, "update": False},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": False},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class OrderMonitor(BaseMonitor):
    """
    Monitor for order data.
    """

    event_type: str = EVENT_ORDER
    data_key: str = "vt_orderid"
    sorting: bool = True

    headers: dict = {
        "orderid": {"display": _("委托号"), "cell": BaseCell, "update": False},
        "reference": {"display": _("来源"), "cell": BaseCell, "update": False},
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "type": {"display": _("类型"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "offset": {"display": _("开平"), "cell": EnumCell, "update": False},
        "price": {"display": _("价格"), "cell": BaseCell, "update": False},
        "volume": {"display": _("总数量"), "cell": BaseCell, "update": True},
        "traded": {"display": _("已成交"), "cell": BaseCell, "update": True},
        "status": {"display": _("状态"), "cell": EnumCell, "update": True},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }

    def init_ui(self) -> None:
        """
        Connect signal.
        """
        super().init_ui()

        self.setToolTip(_("双击单元格撤单"))
        self.itemDoubleClicked.connect(self.cancel_order)

    def cancel_order(self, cell: BaseCell) -> None:
        """
        Cancel order if cell double clicked.
        """
        order: OrderData = cell.get_data()
        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)


class PositionMonitor(BaseMonitor):
    """
    Monitor for position data.
    """

    event_type: str = EVENT_POSITION
    data_key: str = "vt_positionid"
    sorting: bool = True

    headers: dict = {
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "volume": {"display": _("数量"), "cell": BaseCell, "update": True},
        "yd_volume": {"display": _("昨仓"), "cell": BaseCell, "update": True},
        "frozen": {"display": _("冻结"), "cell": BaseCell, "update": True},
        "price": {"display": _("均价"), "cell": BaseCell, "update": True},
        "pnl": {"display": _("盈亏"), "cell": PnlCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class AccountMonitor(BaseMonitor):
    """
    Monitor for account data.
    """

    event_type: str = EVENT_ACCOUNT
    data_key: str = "vt_accountid"
    sorting: bool = True

    headers: dict = {
        "accountid": {"display": _("账号"), "cell": BaseCell, "update": False},
        "balance": {"display": _("余额"), "cell": BaseCell, "update": True},
        "frozen": {"display": _("冻结"), "cell": BaseCell, "update": True},
        "available": {"display": _("可用"), "cell": BaseCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class QuoteMonitor(BaseMonitor):
    """
    Monitor for quote data.
    """

    event_type: str = EVENT_QUOTE
    data_key: str = "vt_quoteid"
    sorting: bool = True

    headers: dict = {
        "quoteid": {"display": _("报价号"), "cell": BaseCell, "update": False},
        "reference": {"display": _("来源"), "cell": BaseCell, "update": False},
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "bid_offset": {"display": _("买开平"), "cell": EnumCell, "update": False},
        "bid_volume": {"display": _("买量"), "cell": BidCell, "update": False},
        "bid_price": {"display": _("买价"), "cell": BidCell, "update": False},
        "ask_price": {"display": _("卖价"), "cell": AskCell, "update": False},
        "ask_volume": {"display": _("卖量"), "cell": AskCell, "update": False},
        "ask_offset": {"display": _("卖开平"), "cell": EnumCell, "update": False},
        "status": {"display": _("状态"), "cell": EnumCell, "update": True},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }

    def init_ui(self) -> None:
        """
        Connect signal.
        """
        super().init_ui()

        self.setToolTip(_("双击单元格撤销报价"))
        self.itemDoubleClicked.connect(self.cancel_quote)

    def cancel_quote(self, cell: BaseCell) -> None:
        """
        Cancel quote if cell double clicked.
        """
        quote: QuoteData = cell.get_data()
        req: CancelRequest = quote.create_cancel_request()
        self.main_engine.cancel_quote(req, quote.gateway_name)


class ConnectDialog(QtWidgets.QDialog):
    """
    Start connection of a certain gateway.
    """

    def __init__(self, main_engine: MainEngine, gateway_name: str) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.gateway_name: str = gateway_name
        self.filename: str = f"connect_{gateway_name.lower()}.json"

        self.widgets: dict[str, tuple[QtWidgets.QWidget, type]] = {}

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("连接{}").format(self.gateway_name))

        # Default setting provides field name, field data type and field default value.
        default_setting: dict | None = self.main_engine.get_default_setting(self.gateway_name)

        # Saved setting provides field data used last time.
        loaded_setting: dict = load_json(self.filename)

        # Initialize line edits and form layout based on setting.
        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()

        if default_setting:
            for field_name, field_value in default_setting.items():
                field_type: type = type(field_value)

                if field_type is list:
                    combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
                    combo.addItems(field_value)

                    if field_name in loaded_setting:
                        saved_value = loaded_setting[field_name]
                        ix: int = combo.findText(saved_value)
                        combo.setCurrentIndex(ix)

                    form.addRow(f"{field_name} <{field_type.__name__}>", combo)
                    self.widgets[field_name] = (combo, field_type)
                else:
                    line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(str(field_value))

                    if field_name in loaded_setting:
                        saved_value = loaded_setting[field_name]
                        line.setText(str(saved_value))

                    if _("密码") in field_name:
                        line.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)

                    if field_type is int:
                        validator: QtGui.QIntValidator = QtGui.QIntValidator()
                        line.setValidator(validator)

                    form.addRow(f"{field_name} <{field_type.__name__}>", line)
                    self.widgets[field_name] = (line, field_type)

        button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("连接"))
        button.clicked.connect(self.connect_gateway)
        form.addRow(button)

        self.setLayout(form)

    def connect_gateway(self) -> None:
        """
        Get setting value from line edits and connect the gateway.
        """
        setting: dict = {}

        for field_name, tp in self.widgets.items():
            widget, field_type = tp
            if field_type is list:
                combo: QtWidgets.QComboBox = cast(QtWidgets.QComboBox, widget)
                field_value = str(combo.currentText())
            else:
                line: QtWidgets.QLineEdit = cast(QtWidgets.QLineEdit, widget)
                try:
                    field_value = field_type(line.text())
                except ValueError:
                    field_value = field_type()
            setting[field_name] = field_value

        save_json(self.filename, setting)

        self.main_engine.connect(setting, self.gateway_name)
        self.accept()


class TradingWidget(QtWidgets.QWidget):
    """
    General manual trading widget.
    """

    signal_tick: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.vt_symbol: str = ""
        self.price_digits: int = 0

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        """"""
        self.setFixedWidth(300)

        # Trading function area
        exchanges: list[Exchange] = self.main_engine.get_all_exchanges()
        self.exchange_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.exchange_combo.addItems([exchange.value for exchange in exchanges])

        self.symbol_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.symbol_line.returnPressed.connect(self.set_vt_symbol)

        self.name_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.name_line.setReadOnly(True)

        self.direction_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.direction_combo.addItems(
            [Direction.LONG.value, Direction.SHORT.value])

        self.offset_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.offset_combo.addItems([offset.value for offset in Offset])

        self.order_type_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.order_type_combo.addItems(
            [order_type.value for order_type in OrderType])

        double_validator: QtGui.QDoubleValidator = QtGui.QDoubleValidator()
        double_validator.setBottom(0)

        self.price_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.price_line.setValidator(double_validator)

        self.volume_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.volume_line.setValidator(double_validator)

        self.gateway_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.gateway_combo.addItems(self.main_engine.get_all_gateway_names())

        self.price_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
        self.price_check.setToolTip(_("设置价格随行情更新"))

        send_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("委托"))
        send_button.clicked.connect(self.send_order)

        cancel_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("全撤"))
        cancel_button.clicked.connect(self.cancel_all)

        grid: QtWidgets.QGridLayout = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel(_("交易所")), 0, 0)
        grid.addWidget(QtWidgets.QLabel(_("代码")), 1, 0)
        grid.addWidget(QtWidgets.QLabel(_("名称")), 2, 0)
        grid.addWidget(QtWidgets.QLabel(_("方向")), 3, 0)
        grid.addWidget(QtWidgets.QLabel(_("开平")), 4, 0)
        grid.addWidget(QtWidgets.QLabel(_("类型")), 5, 0)
        grid.addWidget(QtWidgets.QLabel(_("价格")), 6, 0)
        grid.addWidget(QtWidgets.QLabel(_("数量")), 7, 0)
        grid.addWidget(QtWidgets.QLabel(_("接口")), 8, 0)
        grid.addWidget(self.exchange_combo, 0, 1, 1, 2)
        grid.addWidget(self.symbol_line, 1, 1, 1, 2)
        grid.addWidget(self.name_line, 2, 1, 1, 2)
        grid.addWidget(self.direction_combo, 3, 1, 1, 2)
        grid.addWidget(self.offset_combo, 4, 1, 1, 2)
        grid.addWidget(self.order_type_combo, 5, 1, 1, 2)
        grid.addWidget(self.price_line, 6, 1, 1, 1)
        grid.addWidget(self.price_check, 6, 2, 1, 1)
        grid.addWidget(self.volume_line, 7, 1, 1, 2)
        grid.addWidget(self.gateway_combo, 8, 1, 1, 2)
        grid.addWidget(send_button, 9, 0, 1, 3)
        grid.addWidget(cancel_button, 10, 0, 1, 3)

        # Market depth display area
        bid_color: str = "rgb(255,174,201)"
        ask_color: str = "rgb(160,255,160)"

        self.bp1_label: QtWidgets.QLabel = self.create_label(bid_color)
        self.bp2_label: QtWidgets.QLabel = self.create_label(bid_color)
        self.bp3_label: QtWidgets.QLabel = self.create_label(bid_color)
        self.bp4_label: QtWidgets.QLabel = self.create_label(bid_color)
        self.bp5_label: QtWidgets.QLabel = self.create_label(bid_color)

        self.bv1_label: QtWidgets.QLabel = self.create_label(
            bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv2_label: QtWidgets.QLabel = self.create_label(
            bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv3_label: QtWidgets.QLabel = self.create_label(
            bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv4_label: QtWidgets.QLabel = self.create_label(
            bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv5_label: QtWidgets.QLabel = self.create_label(
            bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self.ap1_label: QtWidgets.QLabel = self.create_label(ask_color)
        self.ap2_label: QtWidgets.QLabel = self.create_label(ask_color)
        self.ap3_label: QtWidgets.QLabel = self.create_label(ask_color)
        self.ap4_label: QtWidgets.QLabel = self.create_label(ask_color)
        self.ap5_label: QtWidgets.QLabel = self.create_label(ask_color)

        self.av1_label: QtWidgets.QLabel = self.create_label(
            ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av2_label: QtWidgets.QLabel = self.create_label(
            ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av3_label: QtWidgets.QLabel = self.create_label(
            ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av4_label: QtWidgets.QLabel = self.create_label(
            ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av5_label: QtWidgets.QLabel = self.create_label(
            ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self.lp_label: QtWidgets.QLabel = self.create_label()
        self.return_label: QtWidgets.QLabel = self.create_label(alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow(self.ap5_label, self.av5_label)
        form.addRow(self.ap4_label, self.av4_label)
        form.addRow(self.ap3_label, self.av3_label)
        form.addRow(self.ap2_label, self.av2_label)
        form.addRow(self.ap1_label, self.av1_label)
        form.addRow(self.lp_label, self.return_label)
        form.addRow(self.bp1_label, self.bv1_label)
        form.addRow(self.bp2_label, self.bv2_label)
        form.addRow(self.bp3_label, self.bv3_label)
        form.addRow(self.bp4_label, self.bv4_label)
        form.addRow(self.bp5_label, self.bv5_label)

        # Overall layout
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(grid)
        vbox.addLayout(form)
        self.setLayout(vbox)

    def create_label(
        self,
        color: str = "",
        alignment: int = QtCore.Qt.AlignmentFlag.AlignLeft
    ) -> QtWidgets.QLabel:
        """
        Create label with certain font color.
        """
        label: QtWidgets.QLabel = QtWidgets.QLabel()
        if color:
            label.setStyleSheet(f"color:{color}")
        label.setAlignment(Qt.AlignmentFlag(alignment))
        return label

    def register_event(self) -> None:
        """"""
        self.signal_tick.connect(self.process_tick_event)
        self.event_engine.register(EVENT_TICK, self.signal_tick.emit)

    def process_tick_event(self, event: Event) -> None:
        """"""
        tick: TickData = event.data
        if tick.vt_symbol != self.vt_symbol:
            return

        price_digits: int = self.price_digits

        self.lp_label.setText(f"{tick.last_price:.{price_digits}f}")
        self.bp1_label.setText(f"{tick.bid_price_1:.{price_digits}f}")
        self.bv1_label.setText(str(tick.bid_volume_1))
        self.ap1_label.setText(f"{tick.ask_price_1:.{price_digits}f}")
        self.av1_label.setText(str(tick.ask_volume_1))

        if tick.pre_close:
            r: float = (tick.last_price / tick.pre_close - 1) * 100
            self.return_label.setText(f"{r:.2f}%")

        if tick.bid_price_2:
            self.bp2_label.setText(f"{tick.bid_price_2:.{price_digits}f}")
            self.bv2_label.setText(str(tick.bid_volume_2))
            self.ap2_label.setText(f"{tick.ask_price_2:.{price_digits}f}")
            self.av2_label.setText(str(tick.ask_volume_2))

            self.bp3_label.setText(f"{tick.bid_price_3:.{price_digits}f}")
            self.bv3_label.setText(str(tick.bid_volume_3))
            self.ap3_label.setText(f"{tick.ask_price_3:.{price_digits}f}")
            self.av3_label.setText(str(tick.ask_volume_3))

            self.bp4_label.setText(f"{tick.bid_price_4:.{price_digits}f}")
            self.bv4_label.setText(str(tick.bid_volume_4))
            self.ap4_label.setText(f"{tick.ask_price_4:.{price_digits}f}")
            self.av4_label.setText(str(tick.ask_volume_4))

            self.bp5_label.setText(f"{tick.bid_price_5:.{price_digits}f}")
            self.bv5_label.setText(str(tick.bid_volume_5))
            self.ap5_label.setText(f"{tick.ask_price_5:.{price_digits}f}")
            self.av5_label.setText(str(tick.ask_volume_5))

        if self.price_check.isChecked():
            self.price_line.setText(f"{tick.last_price:.{price_digits}f}")

    def set_vt_symbol(self) -> None:
        """
        Set the tick depth data to monitor by vt_symbol.
        """
        symbol: str = str(self.symbol_line.text())
        if not symbol:
            return

        # Generate vt_symbol from symbol and exchange
        exchange_value: str = str(self.exchange_combo.currentText())
        vt_symbol: str = f"{symbol}.{exchange_value}"

        if vt_symbol == self.vt_symbol:
            return
        self.vt_symbol = vt_symbol

        # Update name line widget and clear all labels
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
        if not contract:
            self.name_line.setText("")
            gateway_name: str = self.gateway_combo.currentText()
        else:
            self.name_line.setText(contract.name)
            gateway_name = contract.gateway_name

            # Update gateway combo box.
            ix: int = self.gateway_combo.findText(gateway_name)
            self.gateway_combo.setCurrentIndex(ix)

            # Update price digits
            self.price_digits = get_digits(contract.pricetick)

        self.clear_label_text()
        self.volume_line.setText("")
        self.price_line.setText("")

        # Subscribe tick data
        req: SubscribeRequest = SubscribeRequest(
            symbol=symbol, exchange=Exchange(exchange_value)
        )

        self.main_engine.subscribe(req, gateway_name)

    def clear_label_text(self) -> None:
        """
        Clear text on all labels.
        """
        self.lp_label.setText("")
        self.return_label.setText("")

        self.bv1_label.setText("")
        self.bv2_label.setText("")
        self.bv3_label.setText("")
        self.bv4_label.setText("")
        self.bv5_label.setText("")

        self.av1_label.setText("")
        self.av2_label.setText("")
        self.av3_label.setText("")
        self.av4_label.setText("")
        self.av5_label.setText("")

        self.bp1_label.setText("")
        self.bp2_label.setText("")
        self.bp3_label.setText("")
        self.bp4_label.setText("")
        self.bp5_label.setText("")

        self.ap1_label.setText("")
        self.ap2_label.setText("")
        self.ap3_label.setText("")
        self.ap4_label.setText("")
        self.ap5_label.setText("")

    def send_order(self) -> None:
        """
        Send new order manually.
        """
        symbol: str = str(self.symbol_line.text())
        if not symbol:
            QtWidgets.QMessageBox.critical(self, _("委托失败"), _("请输入合约代码"))
            return

        volume_text: str = str(self.volume_line.text())
        if not volume_text:
            QtWidgets.QMessageBox.critical(self, _("委托失败"), _("请输入委托数量"))
            return
        volume: float = float(volume_text)

        price_text: str = str(self.price_line.text())
        if not price_text:
            price: float = 0
        else:
            price = float(price_text)

        req: OrderRequest = OrderRequest(
            symbol=symbol,
            exchange=Exchange(str(self.exchange_combo.currentText())),
            direction=Direction(str(self.direction_combo.currentText())),
            type=OrderType(str(self.order_type_combo.currentText())),
            volume=volume,
            price=price,
            offset=Offset(str(self.offset_combo.currentText())),
            reference="ManualTrading"
        )

        gateway_name: str = str(self.gateway_combo.currentText())

        self.main_engine.send_order(req, gateway_name)

    def cancel_all(self) -> None:
        """
        Cancel all active orders.
        """
        order_list: list[OrderData] = self.main_engine.get_all_active_orders()
        for order in order_list:
            req: CancelRequest = order.create_cancel_request()
            self.main_engine.cancel_order(req, order.gateway_name)

    def update_with_cell(self, cell: BaseCell) -> None:
        """"""
        data = cell.get_data()

        self.symbol_line.setText(data.symbol)
        self.exchange_combo.setCurrentIndex(
            self.exchange_combo.findText(data.exchange.value)
        )

        self.set_vt_symbol()

        if isinstance(data, PositionData):
            if data.direction == Direction.SHORT:
                direction: Direction = Direction.LONG
            elif data.direction == Direction.LONG:
                direction = Direction.SHORT
            else:       # Net position mode
                if data.volume > 0:
                    direction = Direction.SHORT
                else:
                    direction = Direction.LONG

            self.direction_combo.setCurrentIndex(
                self.direction_combo.findText(direction.value)
            )
            self.offset_combo.setCurrentIndex(
                self.offset_combo.findText(Offset.CLOSE.value)
            )
            self.volume_line.setText(str(abs(data.volume)))


class ActiveOrderMonitor(OrderMonitor):
    """
    Monitor which shows active order only.
    """

    def process_event(self, event: Event) -> None:
        """
        Hides the row if order is not active.
        """
        super().process_event(event)

        order: OrderData = event.data
        row_cells: dict = self.cells[order.vt_orderid]
        row: int = self.row(row_cells["volume"])

        if order.is_active():
            self.showRow(row)
        else:
            self.hideRow(row)


class ContractManager(QtWidgets.QWidget):
    """
    Query contract data available to trade in system.
    """

    headers: dict[str, str] = {
        "vt_symbol": _("本地代码"),
        "symbol": _("代码"),
        "exchange": _("交易所"),
        "name": _("名称"),
        "product": _("合约分类"),
        "size": _("合约乘数"),
        "pricetick": _("价格跳动"),
        "min_volume": _("最小委托量"),
        "option_portfolio": _("期权产品"),
        "option_expiry": _("期权到期日"),
        "option_strike": _("期权行权价"),
        "option_type": _("期权类型"),
        "gateway_name": _("交易接口"),
    }

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("合约查询"))
        self.resize(1000, 600)

        self.filter_line: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.filter_line.setPlaceholderText(_("输入合约代码或者交易所，留空则查询所有合约"))

        self.button_show: QtWidgets.QPushButton = QtWidgets.QPushButton(_("查询"))
        self.button_show.clicked.connect(self.show_contracts)

        labels: list = []
        for name, display in self.headers.items():
            label: str = f"{display}\n{name}"
            labels.append(label)

        self.contract_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.contract_table.setColumnCount(len(self.headers))
        self.contract_table.setHorizontalHeaderLabels(labels)
        self.contract_table.verticalHeader().setVisible(False)
        self.contract_table.setEditTriggers(self.contract_table.EditTrigger.NoEditTriggers)
        self.contract_table.setAlternatingRowColors(True)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.filter_line)
        hbox.addWidget(self.button_show)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.contract_table)

        self.setLayout(vbox)

    def show_contracts(self) -> None:
        """
        Show contracts by symbol
        """
        flt: str = str(self.filter_line.text())

        all_contracts: list[ContractData] = self.main_engine.get_all_contracts()
        if flt:
            contracts: list[ContractData] = [
                contract for contract in all_contracts if flt in contract.vt_symbol
            ]
        else:
            contracts = all_contracts

        self.contract_table.clearContents()
        self.contract_table.setRowCount(len(contracts))

        for row, contract in enumerate(contracts):
            for column, name in enumerate(self.headers.keys()):
                value: Any = getattr(contract, name)

                if value in {None, 0}:
                    value = ""

                cell: BaseCell
                if isinstance(value, Enum):
                    cell = EnumCell(value, contract)
                elif isinstance(value, datetime):
                    cell = DateCell(value, contract)
                else:
                    cell = BaseCell(value, contract)
                self.contract_table.setItem(row, column, cell)

        self.contract_table.resizeColumnsToContents()


class AboutDialog(QtWidgets.QDialog):
    """
    Information about the trading platform.
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("关于VeighNa Trader"))

        from ... import __version__ as vnpy_version

        text: str = f"""
            By Traders, For Traders.

            Created by VeighNa Technology


            License：MIT
            Website：www.vnpy.com
            Github：www.github.com/vnpy/vnpy


            VeighNa - {vnpy_version}
            Python - {platform.python_version()}
            PySide6 - {metadata.version("pyside6")}
            NumPy - {metadata.version("numpy")}
            pandas - {metadata.version("pandas")}
            """

        label: QtWidgets.QLabel = QtWidgets.QLabel()
        label.setText(text)
        label.setMinimumWidth(500)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(label)
        self.setLayout(vbox)


class GlobalDialog(QtWidgets.QDialog):
    """
    Start connection of a certain gateway.
    """

    def __init__(self) -> None:
        """"""
        super().__init__()

        self.widgets: dict[str, tuple[QtWidgets.QWidget, type]] = {}
        self._row_widgets: dict[str, tuple[QtWidgets.QWidget, QtWidgets.QWidget, QtWidgets.QGroupBox]] = {}
        self._group_boxes: dict[str, QtWidgets.QGroupBox] = {}
        self._search: QtWidgets.QLineEdit | None = None

        self.init_ui()

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("全局配置"))
        self.setMinimumWidth(800)

        # 获取屏幕尺寸
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        screen_width = screen.width()
        screen_height = screen.height()
        
        # 设置为屏幕的80%，最小800x600
        width = max(int(screen_width * 0.3), 800)
        height = max(int(screen_height * 0.3), 1000)
        
        self.resize(width, height)
        
        # 居中显示
        self.move(
            (screen_width - width) // 2,
            (screen_height - height) // 2
        )

        settings: dict = copy(SETTINGS)
        settings.update(load_json(SETTING_FILENAME))

        search_label: QtWidgets.QLabel = QtWidgets.QLabel(_("搜索"))
        search: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        search.setPlaceholderText(_("按 key/字段名过滤，例如：log、email.port、database"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._apply_filter)
        search_label.setBuddy(search)
        self._search = search

        search_bar: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        search_bar.addWidget(search_label)
        search_bar.addWidget(search)

        content: QtWidgets.QWidget = QtWidgets.QWidget()
        content_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        content_layout.setContentsMargins(2, 2, 2, 2)
        content_layout.setSpacing(4)
        content.setLayout(content_layout)

        def get_group_key(setting_key: str) -> str:
            return setting_key.split(".", 1)[0] if "." in setting_key else "other"

        group_title_map: dict[str, str] = {
            "font": _("字体"),
            "log": _("日志"),
            "email": _("邮件"),
            "datafeed": _("数据源"),
            "database": _("数据库"),
            "other": _("其他"),
        }

        grouped_keys: dict[str, list[str]] = {}
        for key in settings.keys():
            group: str = get_group_key(key)
            grouped_keys.setdefault(group, []).append(key)

        for group, keys in grouped_keys.items():
            title: str = group_title_map.get(group, group)
            group_box: QtWidgets.QGroupBox = QtWidgets.QGroupBox(f"{title} ({group})")
            self._group_boxes[group] = group_box

            form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
            form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
            form.setHorizontalSpacing(16)
            form.setVerticalSpacing(10)
            group_box.setLayout(form)

            for field_name in keys:
                field_value: Any = settings[field_name]
                field_type: type = type(field_value)

                suffix: str = field_name.split(".", 1)[1] if "." in field_name else field_name
                label: QtWidgets.QLabel = QtWidgets.QLabel(suffix)
                label.setToolTip(field_name)

                editor: QtWidgets.QWidget = self._create_editor(field_name, field_value, field_type)
                editor.setToolTip(f"{field_name} <{field_type.__name__}>")

                field_container: QtWidgets.QWidget = QtWidgets.QWidget()
                field_hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
                field_hbox.setContentsMargins(0, 0, 0, 0)
                field_hbox.setSpacing(8)
                field_container.setLayout(field_hbox)
                field_hbox.addWidget(editor, 1)

                type_hint: QtWidgets.QLabel = QtWidgets.QLabel(f"<{field_type.__name__}>")
                type_hint.setObjectName("type_hint")
                type_hint.setStyleSheet("QLabel#type_hint { color: #6B7280; }")
                type_hint.setMinimumWidth(72)
                type_hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                field_hbox.addWidget(type_hint, 0)

                form.addRow(label, field_container)
                self.widgets[field_name] = (editor, field_type)
                self._row_widgets[field_name] = (label, field_container, group_box)

            content_layout.addWidget(group_box)

        content_layout.addStretch(1)

        scroll_area: QtWidgets.QScrollArea = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(content)

        buttons: QtWidgets.QDialogButtonBox = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.update_setting)
        buttons.rejected.connect(self.reject)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(search_bar)
        vbox.addWidget(scroll_area, 1)
        vbox.addWidget(buttons)
        self.setLayout(vbox)

    def update_setting(self) -> None:
        """
        Get setting value from line edits and update global setting file.
        """
        settings: dict = {}
        for field_name, tp in self.widgets.items():
            editor, field_type = tp

            try:
                if isinstance(editor, QtWidgets.QCheckBox):
                    field_value: Any = editor.isChecked()
                elif isinstance(editor, QtWidgets.QSpinBox):
                    field_value = int(editor.value())
                elif isinstance(editor, QtWidgets.QDoubleSpinBox):
                    field_value = float(editor.value())
                elif isinstance(editor, QtWidgets.QLineEdit):
                    value_text: str = editor.text()
                    if field_type is bool:
                        field_value = (value_text == "True")
                    else:
                        field_value = field_type(value_text)
                else:
                    value_text = getattr(editor, "text", lambda: "")()
                    field_value = field_type(str(value_text))
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self,
                    _("输入错误"),
                    _("字段 {} 的值无法转换为 {}：{}").format(field_name, field_type.__name__, str(e)),
                    QtWidgets.QMessageBox.StandardButton.Ok
                )
                return

            settings[field_name] = field_value

        QtWidgets.QMessageBox.information(
            self,
            _("注意"),
            _("全局配置的修改需要重启后才会生效！"),
            QtWidgets.QMessageBox.StandardButton.Ok
        )

        save_json(SETTING_FILENAME, settings)
        self.accept()

    def _create_editor(self, field_name: str, field_value: Any, field_type: type) -> QtWidgets.QWidget:
        """
        Create proper editor widget by python value type, with basic validation.
        """
        if field_type is bool:
            checkbox: QtWidgets.QCheckBox = QtWidgets.QCheckBox()
            checkbox.setChecked(bool(field_value))
            return checkbox

        if field_type is int:
            spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
            if field_name == "font.size":
                spin.setRange(6, 48)
            elif field_name.endswith(".port"):
                spin.setRange(0, 65535)
            elif field_name == "log.level":
                spin.setRange(0, 50)
                spin.setSingleStep(10)
            else:
                spin.setRange(-2147483648, 2147483647)
            spin.setValue(int(field_value))
            return spin

        if field_type is float:
            dspin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox()
            dspin.setRange(-1e12, 1e12)
            dspin.setDecimals(6)
            dspin.setValue(float(field_value))
            return dspin

        line: QtWidgets.QLineEdit = QtWidgets.QLineEdit(str(field_value))
        if "password" in field_name:
            line.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        return line

    def _apply_filter(self, text: str) -> None:
        """
        Filter rows by key or label text. Only toggles visibility to avoid layout churn.
        """
        keyword: str = text.strip().lower()
        visible_count_by_group: dict[QtWidgets.QGroupBox, int] = {}

        for key, (label, field_container, group_box) in self._row_widgets.items():
            label_text: str = ""
            if isinstance(label, QtWidgets.QLabel):
                label_text = label.text()

            hit: bool = (not keyword) or (keyword in key.lower()) or (keyword in label_text.lower())
            label.setVisible(hit)
            field_container.setVisible(hit)
            visible_count_by_group[group_box] = visible_count_by_group.get(group_box, 0) + (1 if hit else 0)

        for group_box, count in visible_count_by_group.items():
            group_box.setVisible(count > 0)


class GatewayConnectionStatusPopup(QtWidgets.QFrame):
    """"""

    def __init__(self, main_engine: MainEngine, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.main_engine: MainEngine = main_engine
        self._rows: dict[str, tuple[QtWidgets.QFrame, QtWidgets.QLabel, QtWidgets.QLabel, QtWidgets.QFrame]] = {}
        self._anchor_window: QtWidgets.QWidget | None = None
        self._anchor_widget: QtWidgets.QWidget | None = None

        self._timer: QtCore.QTimer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)

        self.setWindowFlags(
            QtCore.Qt.WindowType.Popup
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.NoDropShadowWindowHint
        )
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setLineWidth(1)

        self._container: QtWidgets.QWidget = QtWidgets.QWidget()
        self._layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        self._container.setLayout(self._layout)

        wrapper_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.addWidget(self._container)
        self.setLayout(wrapper_layout)

        self.refresh()

    def show_above(self, anchor: QtWidgets.QWidget) -> None:
        self._anchor_window = anchor.window()
        self._anchor_widget = anchor

        self.refresh()
        self.adjustSize()
        self.resize(self.sizeHint())

        anchor_top_left: QtCore.QPoint = anchor.mapToGlobal(QtCore.QPoint(0, 0))
        popup_w: int = self.width()
        popup_h: int = self.height()

        window_geo: QtCore.QRect = self._anchor_window.frameGeometry() if self._anchor_window else anchor.window().frameGeometry()
        left: int = window_geo.left()
        right: int = window_geo.right()
        top: int = window_geo.top()
        bottom: int = window_geo.bottom()

        preferred_x: int = anchor_top_left.x() + anchor.width() - popup_w
        preferred_above: int = anchor_top_left.y() - popup_h - 8
        preferred_below: int = anchor_top_left.y() + anchor.height() + 8

        if preferred_above >= top:
            preferred_y: int = preferred_above
        elif preferred_below + popup_h <= bottom:
            preferred_y = preferred_below
        else:
            preferred_y = preferred_above

        x_min: int = left
        x_max: int = max(left, right - popup_w)
        y_min: int = top
        y_max: int = max(top, bottom - popup_h)

        x: int = min(max(preferred_x, x_min), x_max)
        y: int = min(max(preferred_y, y_min), y_max)

        self.move(x, y)
        self.show()
        self.raise_()
        self._timer.start()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._timer.stop()
        self._anchor_window = None
        self._anchor_widget = None
        super().hideEvent(event)

    def refresh(self) -> None:
        names: list[str] = list(self.main_engine.gateways.keys())

        if not names:
            self._ensure_empty_placeholder()
            return

        self._remove_empty_placeholder()

        existing: set[str] = set(self._rows.keys())
        desired: set[str] = set(names)

        for name in list(existing - desired):
            self._remove_row(name)

        for name in names:
            if name not in self._rows:
                self._add_row(name)
            self._update_row(name)

        self.adjustSize()
        if self.isVisible() and self._anchor_window:
            self.resize(self.sizeHint())
            self._clamp_into_window(self._anchor_window)

    def _ensure_empty_placeholder(self) -> None:
        if "__empty__" in self._rows:
            return

        group, name_label, exchanges_label, dot = self._create_group("__empty__")
        name_label.setText(_("未加载任何接口"))
        name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        exchanges_label.setVisible(False)
        group.setProperty("connected", False)
        dot.setProperty("connected", False)

        self._layout.addWidget(group)
        self._rows["__empty__"] = (group, name_label, exchanges_label, dot)

        self.adjustSize()

    def _remove_empty_placeholder(self) -> None:
        if "__empty__" not in self._rows:
            return
        self._remove_row("__empty__")

    def _add_row(self, gateway_name: str) -> None:
        group, name_label, exchanges_label, dot = self._create_group(gateway_name)
        name_label.setText(gateway_name)
        exchanges_label.setText("")
        group.setProperty("connected", False)
        dot.setProperty("connected", False)

        self._layout.addWidget(group)
        self._rows[gateway_name] = (group, name_label, exchanges_label, dot)

    def _update_row(self, gateway_name: str) -> None:
        gateway = self.main_engine.gateways.get(gateway_name)
        connected: bool = bool(getattr(gateway, "connected", False))

        group, name_label, exchanges_label, dot = self._rows[gateway_name]

        name_label.setText(gateway_name)

        exchanges_label.setText(self._format_exchanges(getattr(gateway, "exchanges", None)))

        group.setProperty("connected", connected)
        dot.setProperty("connected", connected)
        self._repolish(group)
        self._repolish(dot)

    def _remove_row(self, gateway_name: str) -> None:
        row = self._rows.pop(gateway_name, None)
        if not row:
            return
        group, name_label, exchanges_label, dot = row
        self._layout.removeWidget(group)
        group.setParent(None)
        group.deleteLater()

    def _create_group(
        self,
        gateway_name: str,
    ) -> tuple[QtWidgets.QFrame, QtWidgets.QLabel, QtWidgets.QLabel, QtWidgets.QFrame]:
        group: QtWidgets.QFrame = QtWidgets.QFrame()
        group.setObjectName("gateway_status_group")
        group.setProperty("gateway_name", gateway_name)

        name_label: QtWidgets.QLabel = QtWidgets.QLabel()
        name_label.setObjectName("gateway_name")

        exchanges_label: QtWidgets.QLabel = QtWidgets.QLabel()
        exchanges_label.setObjectName("gateway_exchanges")

        left: QtWidgets.QWidget = QtWidgets.QWidget()
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(1)
        vbox.addWidget(name_label)
        vbox.addWidget(exchanges_label)
        left.setLayout(vbox)

        dot: QtWidgets.QFrame = QtWidgets.QFrame()
        dot.setObjectName("gateway_status_dot")
        dot.setFixedSize(10, 10)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.setContentsMargins(8, 6, 8, 6)
        hbox.setSpacing(10)
        hbox.addWidget(left, 1)
        hbox.addWidget(dot, 0, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        group.setLayout(hbox)

        return group, name_label, exchanges_label, dot

    def _format_exchanges(self, exchanges: object) -> str:
        if exchanges is None:
            return "-"

        if isinstance(exchanges, (str, bytes)):
            text: str = str(exchanges).strip()
            return text if text else "-"

        if not isinstance(exchanges, Iterable):
            return "-"

        parts: list[str] = []
        for ex in exchanges:
            value = getattr(ex, "value", None)
            parts.append(str(value) if value is not None else str(ex))

        result: str = ", ".join([p for p in parts if p])
        return result if result else "-"

    def _clamp_into_window(self, window: QtWidgets.QWidget) -> None:
        window_geo: QtCore.QRect = window.frameGeometry()
        left: int = window_geo.left()
        right: int = window_geo.right()
        top: int = window_geo.top()
        bottom: int = window_geo.bottom()

        popup_w: int = self.width()
        popup_h: int = self.height()

        x_min: int = left
        x_max: int = max(left, right - popup_w)
        y_min: int = top
        y_max: int = max(top, bottom - popup_h)

        pos: QtCore.QPoint = self.pos()
        x: int = min(max(pos.x(), x_min), x_max)
        y: int = min(max(pos.y(), y_min), y_max)

        if x != pos.x() or y != pos.y():
            self.move(x, y)

    def _repolish(self, widget: QtWidgets.QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()


class GatewayConnectionStatusButton(QtWidgets.QToolButton):
    """"""

    def __init__(self, main_engine: MainEngine, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.main_engine: MainEngine = main_engine
        self._popup: GatewayConnectionStatusPopup = GatewayConnectionStatusPopup(main_engine, self)

        self.setText(_("网关状态"))
        self.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setAutoRaise(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._toggle_popup)

        self._update_icon()

        self._aggregate_timer: QtCore.QTimer = QtCore.QTimer(self)
        self._aggregate_timer.setInterval(1500)
        self._aggregate_timer.timeout.connect(self._update_icon)
        self._aggregate_timer.start()

    def _toggle_popup(self) -> None:
        if self._popup.isVisible():
            self._popup.hide()
            return
        self._popup.show_above(self)

    def _update_icon(self) -> None:
        gateways = list(self.main_engine.gateways.values())
        if not gateways:
            self.setIcon(self._make_dot_icon(QtGui.QColor("#EF4444")))
            return

        connected_count: int = 0
        for gateway in gateways:
            if bool(getattr(gateway, "connected", False)):
                connected_count += 1

        if connected_count == 0:
            color = QtGui.QColor("#EF4444")
        elif connected_count == len(gateways):
            color = QtGui.QColor("#22C55E")
        else:
            color = QtGui.QColor("#F59E0B")

        self.setIcon(self._make_dot_icon(color))

    def _make_dot_icon(self, color: QtGui.QColor) -> QtGui.QIcon:
        size: int = 10
        pm: QtGui.QPixmap = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.GlobalColor.transparent)

        painter: QtGui.QPainter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(color))
        painter.drawEllipse(0, 0, size - 1, size - 1)
        painter.end()

        return QtGui.QIcon(pm)
