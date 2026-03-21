import traceback
from dataclasses import dataclass
from datetime import datetime
from threading import Thread
from typing import cast

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed

from .locale_ import _
from .tushare_datafeed import TushareDatafeedPro, DATA_DIR

APP_NAME = "TusharePro"

EVENT_TUSHAREPRO_LOG = "eTushareProLog"
EVENT_TUSHAREPRO_PROGRESS = "eTushareProProgress"
EVENT_TUSHAREPRO_TASK_FINISHED = "eTushareProTaskFinished"


@dataclass(frozen=True)
class TaskProgress:
    percent: int
    message: str


@dataclass(frozen=True)
class TaskFinished:
    success: bool
    message: str


class TushareProEngine(BaseEngine):
    """
    For running CTA strategy backtesting.
    """

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.datafeed: BaseDatafeed = get_datafeed()
        self.thread: Thread | None = None

    def init_engine(self) -> None:
        result: bool = self.datafeed.init(self.write_log)
        if result:
            self.write_log(_("数据服务初始化成功"))
        else:
            self.write_log(_("数据服务初始化失败"))

    def write_log(self, msg: str) -> None:
        event: Event = Event(EVENT_TUSHAREPRO_LOG)
        event.data = msg
        self.event_engine.put(event)

    def put_progress(self, percent: int, message: str) -> None:
        event: Event = Event(EVENT_TUSHAREPRO_PROGRESS)
        event.data = TaskProgress(percent=percent, message=message)
        self.event_engine.put(event)

    def put_task_finished(self, success: bool, message: str) -> None:
        event: Event = Event(EVENT_TUSHAREPRO_TASK_FINISHED)
        event.data = TaskFinished(success=success, message=message)
        self.event_engine.put(event)

    def _is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _get_tushare_datafeed(self) -> TushareDatafeedPro:
        return cast(TushareDatafeedPro, self.datafeed)

    def download_all_history(self, start_date: str, end_date: str) -> None:
        if self._is_running():
            self.write_log(_("已有任务正在运行"))
            return

        def run() -> None:
            try:
                self.put_progress(5, _("开始全量下载"))
                df = self._get_tushare_datafeed().query_all_stock_history(
                    start_date=start_date,
                    end_date=end_date,
                    output=self.write_log
                )
                if df is None:
                    self.put_progress(100, _("全量下载失败"))
                    self.put_task_finished(False, _("全量下载失败"))
                    return
                self.put_progress(100, _("全量下载完成"))
                self.put_task_finished(True, _("全量下载完成"))
            except Exception:
                self.write_log(traceback.format_exc())
                self.put_progress(100, _("全量下载异常"))
                self.put_task_finished(False, _("全量下载异常"))

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def update_incremental(self, end_date: str | None = None) -> None:
        if self._is_running():
            self.write_log(_("已有任务正在运行"))
            return

        def run() -> None:
            try:
                self.put_progress(5, _("开始增量更新"))
                df = self._get_tushare_datafeed().update_all_stock_history(
                    end_date=end_date,
                    output=self.write_log
                )
                if df is None:
                    self.put_progress(100, _("增量更新失败"))
                    self.put_task_finished(False, _("增量更新失败"))
                    return
                self.put_progress(100, _("增量更新完成"))
                self.put_task_finished(True, _("增量更新完成"))
            except Exception:
                print(traceback.format_exc())
                self.write_log(traceback.format_exc())
                self.put_progress(100, _("增量更新异常"))
                self.put_task_finished(False, _("增量更新异常"))

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def set_post_close_time(self, time_str: str) -> None:
        try:
            self._get_tushare_datafeed().set_post_close_update_time(time_str)
            self.write_log(_("已设置盘后更新时间：{}").format(time_str))
        except Exception:
            self.write_log(traceback.format_exc())
            self.put_task_finished(False, _("设置盘后更新时间失败"))

    def run_post_close_update_now(self) -> None:
        if self._is_running():
            self.write_log(_("已有任务正在运行"))
            return

        def run() -> None:
            try:
                today = datetime.now().strftime("%Y%m%d")
                if not self._get_tushare_datafeed().downloader.is_trade_date(today):
                    self.write_log(_("非交易日，跳过盘后更新"))
                    self.put_progress(100, _("已跳过"))
                    self.put_task_finished(True, _("非交易日已跳过"))
                    return
                self.put_progress(5, _("开始盘后更新"))
                df = self._get_tushare_datafeed().update_all_stock_history(
                    end_date=None,
                    output=self.write_log
                )
                if df is None:
                    self.put_progress(100, _("盘后更新失败"))
                    self.put_task_finished(False, _("盘后更新失败"))
                    return
                self.put_progress(100, _("盘后更新完成"))
                self.put_task_finished(True, _("盘后更新完成"))
            except Exception:
                self.write_log(traceback.format_exc())
                self.put_progress(100, _("盘后更新异常"))
                self.put_task_finished(False, _("盘后更新异常"))

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()
