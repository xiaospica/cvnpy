from datetime import datetime
from pathlib import Path
from typing import cast
import time

from vnpy.trader.ui import QtCore, QtWidgets, QtGui
from vnpy.trader.engine import MainEngine
from vnpy.event import Event, EventEngine

from ..locale_ import _
from ..engine import (
    APP_NAME,
    EVENT_TUSHAREPRO_LOG,
    EVENT_TUSHAREPRO_PROGRESS,
    EVENT_TUSHAREPRO_TASK_FINISHED,
    TaskFinished,
    TaskProgress,
    TushareProEngine,
    DATA_DIR
)


class TushareProManager(QtWidgets.QWidget):
    signal_log: QtCore.Signal = QtCore.Signal(Event)
    signal_progress: QtCore.Signal = QtCore.Signal(Event)
    signal_task_finished: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.engine: TushareProEngine = cast(TushareProEngine, self.main_engine.get_engine(APP_NAME))

        self.init_ui()
        self.register_event()
        self.engine.init_engine()
    

    def init_ui(self) -> None:
        """"""
        self.setWindowTitle(_("TusharePro"))

        self.start_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDate(QtCore.QDate(2005, 1, 4))

        self.end_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        self.end_date_edit.setCalendarPopup(True)
        self.end_date_edit.setDate(QtCore.QDate.currentDate())

        self.incremental_end_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit()
        self.incremental_end_date_edit.setCalendarPopup(True)
        self.incremental_end_date_edit.setDate(QtCore.QDate.currentDate())

        self.download_all_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("开始全量下载"))
        self.download_all_button.clicked.connect(self.start_download_all)

        self.update_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("开始增量更新"))
        self.update_button.clicked.connect(self.start_update_incremental)

        self.time_edit: QtWidgets.QTimeEdit = QtWidgets.QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QtCore.QTime(19, 0))

        self.apply_time_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("应用时间"))
        self.apply_time_button.clicked.connect(self.apply_post_close_time)

        self.run_now_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("立即执行一次"))
        self.run_now_button.clicked.connect(self.run_post_close_now)

        self.status_label: QtWidgets.QLabel = QtWidgets.QLabel(_("Idle"))
        self.progress_bar: QtWidgets.QProgressBar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.log_monitor: QtWidgets.QTextEdit = QtWidgets.QTextEdit()
        self.log_monitor.setReadOnly(True)

        self.clear_log_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("清空日志"))
        self.clear_log_button.clicked.connect(self.log_monitor.clear)

        self.overview_text: QtWidgets.QTextEdit = QtWidgets.QTextEdit()
        self.overview_text.setReadOnly(True)
        self.refresh_overview_button: QtWidgets.QPushButton = QtWidgets.QPushButton(_("刷新概览"))
        self.refresh_overview_button.clicked.connect(self.refresh_overview)

        download_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox(_("全量下载"))
        download_form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        download_form.addRow(_("开始日期"), self.start_date_edit)
        download_form.addRow(_("结束日期"), self.end_date_edit)
        download_form.addRow(self.download_all_button)
        download_group.setLayout(download_form)

        incremental_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox(_("增量更新"))
        incremental_form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        incremental_form.addRow(_("截止日期"), self.incremental_end_date_edit)
        incremental_form.addRow(self.update_button)
        incremental_group.setLayout(incremental_form)

        schedule_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox(_("盘后自动更新"))
        schedule_form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        schedule_form.addRow(_("执行时间"), self.time_edit)

        schedule_buttons: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        schedule_buttons.addWidget(self.apply_time_button)
        schedule_buttons.addWidget(self.run_now_button)
        schedule_form.addRow(schedule_buttons)
        schedule_group.setLayout(schedule_form)

        status_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox(_("状态"))
        status_form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        status_form.addRow(_("当前状态"), self.status_label)
        status_form.addRow(_("进度"), self.progress_bar)
        status_group.setLayout(status_form)

        left: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        left.addWidget(download_group)
        left.addWidget(incremental_group)
        left.addWidget(schedule_group)
        left.addWidget(status_group)
        left.addStretch()

        left_widget: QtWidgets.QWidget = QtWidgets.QWidget()
        left_widget.setLayout(left)
        left_widget.setFixedWidth(360)

        log_tab: QtWidgets.QWidget = QtWidgets.QWidget()
        log_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        log_layout.addWidget(self.log_monitor)
        log_layout.addWidget(self.clear_log_button)
        log_tab.setLayout(log_layout)

        overview_tab: QtWidgets.QWidget = QtWidgets.QWidget()
        overview_layout: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        overview_layout.addWidget(self.overview_text)
        overview_layout.addWidget(self.refresh_overview_button)
        overview_tab.setLayout(overview_layout)

        tabs: QtWidgets.QTabWidget = QtWidgets.QTabWidget()
        tabs.addTab(log_tab, _("日志"))
        tabs.addTab(overview_tab, _("数据概览"))

        main_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        main_layout.addWidget(left_widget)
        main_layout.addWidget(tabs)
        self.setLayout(main_layout)

        self.refresh_overview()

    def register_event(self) -> None:
        self.signal_log.connect(self.process_log_event)
        self.signal_progress.connect(self.process_progress_event)
        self.signal_task_finished.connect(self.process_task_finished_event)

        self.event_engine.register(EVENT_TUSHAREPRO_LOG, self.signal_log.emit)
        self.event_engine.register(EVENT_TUSHAREPRO_PROGRESS, self.signal_progress.emit)
        self.event_engine.register(EVENT_TUSHAREPRO_TASK_FINISHED, self.signal_task_finished.emit)

    def append_log(self, msg: str) -> None:
        is_error = "Traceback" in msg or "❌" in msg or "异常" in msg or "失败" in msg or "error" in msg
        if is_error:
            color = QtGui.QColor("#D64541")
            self.log_monitor.setTextColor(color)
        self.log_monitor.append(f'[{time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}]: '+msg)
        self.log_monitor.moveCursor(QtGui.QTextCursor.End)

    def set_running(self, running: bool) -> None:
        self.download_all_button.setEnabled(not running)
        self.update_button.setEnabled(not running)
        self.run_now_button.setEnabled(not running)

    def process_log_event(self, event: Event) -> None:
        msg: str = str(event.data)
        self.append_log(msg)

    def process_progress_event(self, event: Event) -> None:
        progress: TaskProgress = cast(TaskProgress, event.data)
        self.progress_bar.setValue(int(progress.percent))
        self.status_label.setText(progress.message)
        if progress.percent < 100:
            self.status_label.setStyleSheet("color: #2D7DD2;")

    def process_task_finished_event(self, event: Event) -> None:
        finished: TaskFinished = cast(TaskFinished, event.data)
        if finished.success:
            self.status_label.setStyleSheet("color: #2E7D32;")
        else:
            self.status_label.setStyleSheet("color: #D64541;")
            QtWidgets.QMessageBox.critical(self, _("任务失败"), finished.message)
        self.status_label.setText(finished.message)
        self.progress_bar.setValue(100)
        self.set_running(False)
        self.refresh_overview()

    def start_download_all(self) -> None:
        start_date = self.start_date_edit.date().toString("yyyyMMdd")
        end_date = self.end_date_edit.date().toString("yyyyMMdd")
        self.progress_bar.setValue(0)
        self.set_running(True)
        self.engine.download_all_history(start_date, end_date)

    def start_update_incremental(self) -> None:
        end_date = self.incremental_end_date_edit.date().toString("yyyyMMdd")
        self.progress_bar.setValue(0)
        self.set_running(True)
        self.engine.update_incremental(end_date=end_date)

    def apply_post_close_time(self) -> None:
        time_str = self.time_edit.time().toString("HH:mm")
        self.engine.set_post_close_time(time_str)

    def run_post_close_now(self) -> None:
        self.progress_bar.setValue(0)
        self.set_running(True)
        self.engine.run_post_close_update_now()

    def refresh_overview(self) -> None:
        data_path = DATA_DIR / "df_all_stock.parquet"
        if not data_path.exists():
            self.overview_text.setPlainText(_("未找到本地数据文件：{}").format(str(data_path)))
            return
        try:
            import pyarrow.parquet as pq

            pf = pq.ParquetFile(str(data_path))
            text = []
            text.append(_("文件：{}").format(str(data_path)))
            text.append(_("行数：{}").format(pf.metadata.num_rows))
            text.append(_("列数：{}").format(pf.metadata.num_columns))

            target_column = 'trade_date'

            column_idx = pf.schema.names.index(target_column)
            stats = pf.metadata.row_group(0).column(column_idx).statistics

            if stats and stats.has_min_max:
                min_date = stats.min
                max_date = stats.max
                text.append(_("\n列 [{}] 日期范围：{} ~ {}").format(target_column, min_date, max_date))
            else:
                text.append(_("\n列 [{}] 无统计信息，需读取数据").format(target_column))

            self.overview_text.setPlainText("\n".join(text))
        except Exception as e:
            self.overview_text.setPlainText(_("读取概览失败：{}").format(str(e)))
