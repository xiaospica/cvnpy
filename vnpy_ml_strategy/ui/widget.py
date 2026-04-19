"""vnpy_ml_strategy UI widget.

vnpy 的 ``MainWindow.init_menu`` 要求每个 app 必须暴露一个 ``ui.<widget_name>``
作为菜单项. 本 app 的主要监控交互在 mlearnweb 前端 Tab2 完成(浏览器端,
不用 Qt), 这里只留一个最小占位页给"功能"菜单 — 点开后展示当前已注册的
ML 策略与最新一次推理状态,避免 vnpy 启动时 ModuleNotFoundError.

真需要 Qt 监控时再扩展这个 widget(读 ``MLEngine`` 的 MetricsCache).
"""

from __future__ import annotations

from typing import cast

from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.engine import MainEngine
from vnpy.event import EventEngine

from ..base import APP_NAME
from ..engine import MLEngine


class MLStrategyManager(QtWidgets.QWidget):
    """占位管理器窗口。Tab2 监控面板走 mlearnweb 前端,这里仅展示策略状态."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()
        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.engine: MLEngine = cast(MLEngine, self.main_engine.get_engine(APP_NAME))

        self._init_ui()
        self._refresh_btn_clicked()

        # 秒级心跳刷新,避免用户看到过期状态
        self._timer = QtCore.QTimer()
        self._timer.timeout.connect(self._refresh_btn_clicked)
        self._timer.start(5000)

    def _init_ui(self) -> None:
        self.setWindowTitle("ML 策略管理")
        self.resize(720, 420)

        note = QtWidgets.QLabel(
            "ML 策略的完整监控面板在 mlearnweb 浏览器端(Tab2).\n"
            "本窗口仅展示当前 vnpy 进程内已注册的 ML 策略状态."
        )
        note.setWordWrap(True)

        self.refresh_btn = QtWidgets.QPushButton("刷新")
        self.refresh_btn.clicked.connect(self._refresh_btn_clicked)

        self.table = QtWidgets.QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            "策略", "最新运行日", "状态", "预测数", "耗时(ms)", "model_run_id"
        ])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(note, 1)
        top.addWidget(self.refresh_btn)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(top)
        layout.addWidget(self.table)
        self.setLayout(layout)

    def _refresh_btn_clicked(self) -> None:
        strategies = getattr(self.engine, "strategies", {})
        self.table.setRowCount(len(strategies))
        for row, (name, strat) in enumerate(strategies.items()):
            values = [
                name,
                getattr(strat, "last_run_date", "") or "—",
                getattr(strat, "last_status", "") or "—",
                str(getattr(strat, "last_n_pred", 0) or 0),
                str(getattr(strat, "last_duration_ms", 0) or 0),
                (getattr(strat, "last_model_run_id", "") or "—")[:12],
            ]
            for col, v in enumerate(values):
                self.table.setItem(row, col, QtWidgets.QTableWidgetItem(str(v)))

    def closeEvent(self, event) -> None:  # noqa: D401
        self._timer.stop()
        event.accept()
