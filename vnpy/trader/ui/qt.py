import ctypes
import platform
import sys
import traceback
import webbrowser
import types
import threading
from pathlib import Path

import qdarkstyle  # type: ignore
from PySide6 import QtGui, QtWidgets, QtCore
from loguru import logger

from ..setting import SETTINGS
from ..utility import get_icon_path
from ..locale import _


Qt = QtCore.Qt


def _get_resources_dir() -> Path:
    return Path(__file__).resolve().parents[3].joinpath("resources")


def apply_system_theme_stylesheet(qapp: QtWidgets.QApplication) -> None:
    resources_dir: Path = _get_resources_dir()

    dark_qss: Path = resources_dir.joinpath("darkstyle.qss")
    light_qss: Path = resources_dir.joinpath("lightstyle.qss")

    if not dark_qss.exists() or not light_qss.exists():
        return

    is_dark: bool = qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
    qss_path: Path = dark_qss if is_dark else light_qss

    qapp.setStyleSheet(qss_path.read_text(encoding="utf-8"))


def apply_system_theme_palette(qapp: QtWidgets.QApplication) -> None:
    is_dark: bool = qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
    palette: QtGui.QPalette = qapp.palette()

    if is_dark:
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(53, 53, 53))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(187, 187, 187))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.WindowText,
            QtGui.QColor(127, 127, 127),
        )
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(42, 42, 42))
        palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(66, 66, 66))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(187, 187, 187))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(187, 187, 187))
        palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(187, 187, 187))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.Text,
            QtGui.QColor(127, 127, 127),
        )
        palette.setColor(QtGui.QPalette.ColorRole.Dark, QtGui.QColor(35, 35, 35))
        palette.setColor(QtGui.QPalette.ColorRole.Shadow, QtGui.QColor(20, 20, 20))
        palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(53, 53, 53))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(187, 187, 187))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.ButtonText,
            QtGui.QColor(127, 127, 127),
        )
        palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor(255, 0, 0))
        palette.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor(42, 130, 218))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(42, 130, 218))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.Highlight,
            QtGui.QColor(80, 80, 80),
        )
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(187, 187, 187))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.HighlightedText,
            QtGui.QColor(127, 127, 127),
        )
    else:
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(240, 240, 240))
        palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(0, 0, 0))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.WindowText,
            QtGui.QColor(120, 120, 120),
        )
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(255, 255, 255))
        palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(233, 231, 227))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(255, 255, 220))
        palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(0, 0, 0))
        palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(0, 0, 0))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.Text,
            QtGui.QColor(120, 120, 120),
        )
        palette.setColor(QtGui.QPalette.ColorRole.Dark, QtGui.QColor(160, 160, 160))
        palette.setColor(QtGui.QPalette.ColorRole.Shadow, QtGui.QColor(105, 105, 105))
        palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(240, 240, 240))
        palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(0, 0, 0))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.ButtonText,
            QtGui.QColor(120, 120, 120),
        )
        palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtGui.QColor(0, 0, 255))
        palette.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor(51, 153, 255))
        palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(0, 0, 255))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.Highlight,
            QtGui.QColor(51, 153, 255),
        )
        palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255, 255, 255))
        palette.setColor(
            QtGui.QPalette.ColorGroup.Disabled,
            QtGui.QPalette.ColorRole.HighlightedText,
            QtGui.QColor(255, 255, 255),
        )

    qapp.setPalette(palette)


def apply_accent_palette(qapp: QtWidgets.QApplication) -> None:
    accent_color: str = str(SETTINGS.get("ui.accent_color", "")).strip()
    if not accent_color:
        return

    color: QtGui.QColor = QtGui.QColor(accent_color)
    if not color.isValid():
        return

    palette: QtGui.QPalette = qapp.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, color)
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#FFFFFF"))
    qapp.setPalette(palette)


def create_qapp(app_name: str = "VeighNa Trader") -> QtWidgets.QApplication:
    """
    Create Qt Application.
    """
    # Set up dark stylesheet
    qapp: QtWidgets.QApplication = QtWidgets.QApplication(sys.argv)
    # qapp.setStyleSheet(qdarkstyle.load_stylesheet(qt_api="pyside6"))

    # Set up font
    font: QtGui.QFont = QtGui.QFont(SETTINGS["font.family"], SETTINGS["font.size"])
    qapp.setFont(font)

    def _sync_theme() -> None:
        apply_system_theme_palette(qapp)
        apply_accent_palette(qapp)
        apply_system_theme_stylesheet(qapp)

    _sync_theme()
    qapp.styleHints().colorSchemeChanged.connect(lambda _: _sync_theme())

    # Set up icon
    icon: QtGui.QIcon = QtGui.QIcon(get_icon_path(__file__, "vnpy.ico"))
    qapp.setWindowIcon(icon)

    # Set up windows process ID
    if "Windows" in platform.uname():
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            app_name
        )

    # Exception Handling
    exception_widget: ExceptionWidget = ExceptionWidget()

    def excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: types.TracebackType | None
    ) -> None:
        """Show exception detail with QMessageBox."""
        logger.opt(exception=(exc_type, exc_value, exc_traceback)).critical("Main thread exception")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

        msg: str = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        exception_widget.signal.emit(msg)

    sys.excepthook = excepthook

    def threading_excepthook(args: threading.ExceptHookArgs) -> None:
        """Show exception detail from background threads with QMessageBox."""
        if args.exc_value and args.exc_traceback:
            logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical("Background thread exception")
            sys.__excepthook__(args.exc_type, args.exc_value, args.exc_traceback)

        msg: str = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        exception_widget.signal.emit(msg)

    threading.excepthook = threading_excepthook

    return qapp


class ExceptionWidget(QtWidgets.QWidget):
    """"""
    signal: QtCore.Signal = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        """"""
        super().__init__(parent)

        self.init_ui()
        self.signal.connect(self.show_exception)

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("触发异常"))
        self.setFixedSize(600, 600)

        self.msg_edit: QtWidgets.QTextEdit = QtWidgets.QTextEdit()
        self.msg_edit.setReadOnly(True)

        copy_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("复制"))
        copy_button.clicked.connect(self._copy_text)

        community_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("求助"))
        community_button.clicked.connect(self._open_community)

        close_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("关闭"))
        close_button.clicked.connect(self.close)

        hbox: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox.addWidget(copy_button)
        hbox.addWidget(community_button)
        hbox.addWidget(close_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.msg_edit)
        vbox.addLayout(hbox)

        self.setLayout(vbox)

    def show_exception(self, msg: str) -> None:
        """"""
        self.msg_edit.setText(msg)
        self.show()

    def _copy_text(self) -> None:
        """"""
        self.msg_edit.selectAll()
        self.msg_edit.copy()

    def _open_community(self) -> None:
        """"""
        webbrowser.open("https://www.vnpy.com/forum/forum/2-ti-wen-qiu-zhu")
