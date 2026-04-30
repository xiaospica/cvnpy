from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger


@dataclass(frozen=True)
class DailyJobConfig:
    job_id: str
    time_str: str


def _parse_hhmm(time_str: str) -> tuple[int, int]:
    parts = time_str.split(":")
    if len(parts) != 2:
        raise ValueError(f"非法时间格式: {time_str}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"非法时间取值: {time_str}")
    return hour, minute


class DailyTimeTaskScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.tz = ZoneInfo(timezone)
        self._scheduler = BackgroundScheduler(timezone=self.tz)
        self._lock = threading.RLock()
        self._started = False
        self._job_wrapped_funcs: dict[str, Callable[[], None]] = {}

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._scheduler.start()
            self._started = True

    def stop(self, wait: bool = False) -> None:
        with self._lock:
            if not self._started:
                return
            self._scheduler.shutdown(wait=wait)
            self._started = False

    def register_daily_job(
        self,
        name: str,
        time_str: str,
        job_func: Callable[[], None],
        misfire_grace_time: int = 3600,
    ) -> None:
        hour, minute = _parse_hhmm(time_str)

        def wrapped(**kwargs) -> None:
            from datetime import datetime as _dt
            run_date = _dt.now().strftime("%Y-%m-%d")
            try:
                logger.info(f"[scheduler] start job({name}) at {run_date} {time_str} kwargs={kwargs}")
                job_func(**kwargs)
                logger.info(f"[scheduler] job done({name})")
            except Exception as e:
                logger.exception(f"[scheduler] job failed({name}): {e}")

        trigger = CronTrigger(hour=hour, minute=minute)
        with self._lock:
            self._job_wrapped_funcs[name] = wrapped
            self._scheduler.add_job(
                wrapped,
                trigger=trigger,
                id=name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=misfire_grace_time,
            )

    def update_job_time(self, name: str, time_str: str) -> None:
        hour, minute = _parse_hhmm(time_str)
        trigger = CronTrigger(hour=hour, minute=minute)
        with self._lock:
            job = self._scheduler.get_job(name)
            if job is None:
                raise KeyError(f"任务不存在: {name}")
            self._scheduler.reschedule_job(name, trigger=trigger)

    def run_job_now(self, name: str, **kwargs) -> None:
        """立即同步执行已注册 job，透传 kwargs 给 job_func。

        实盘 cron 触发：wrapped() 无 kwargs → job_func() 走 today。
        回放手动触发：wrapped(as_of_date=day) → job_func(as_of_date=day) 走指定逻辑日。
        """
        with self._lock:
            func = self._job_wrapped_funcs.get(name)
        if func is None:
            raise KeyError(f"任务不存在: {name}")
        func(**kwargs)

    def pause_job(self, name: str) -> None:
        """暂停指定 cron job（按 job name 精确隔离，不影响其他 job）。

        Phase 4 回放期间用：仅暂停本策略自己的 trigger_time，避免回放与
        实时推理并发起两个推理子进程。回放完成后调 resume_job 恢复。
        """
        with self._lock:
            self._scheduler.pause_job(name)

    def resume_job(self, name: str) -> None:
        with self._lock:
            self._scheduler.resume_job(name)

    def get_job_next_run_time(self, name: str):
        """返回 job 下次触发时间。暂停时返回 None。供测试断言用。"""
        with self._lock:
            job = self._scheduler.get_job(name)
        return job.next_run_time if job else None
