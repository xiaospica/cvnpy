"""A 股交易日历封装.

优先级:
1. 优先用本地 qlib bin 的 calendar 文件 (``qlib_data_bin/calendars/day.txt``) —
   只读磁盘文件, 无需 import qlib, 不会把 qlib 拖进 vnpy 主进程
2. 如果 calendar 文件不存在, fallback 到 weekday < 5 (周一至周五)

vnpy 主进程每天盘前 09:15 先调 ``is_trade_day`` 做一次短路, 非交易日不启
subprocess 节省开机成本.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional, Set


class QlibCalendar:
    """从 qlib_data_bin/calendars/day.txt 读交易日.

    该文件每行一个 ``YYYY-MM-DD`` 字符串, 仅含交易日.
    """

    def __init__(self, provider_uri: str):
        self._provider_uri = provider_uri
        self._trade_days: Optional[Set[str]] = None

    def _load(self) -> Set[str]:
        if self._trade_days is not None:
            return self._trade_days
        cal_path = Path(self._provider_uri) / "calendars" / "day.txt"
        if not cal_path.exists():
            self._trade_days = set()
            return self._trade_days
        lines = cal_path.read_text(encoding="utf-8").splitlines()
        self._trade_days = {line.strip() for line in lines if line.strip()}
        return self._trade_days

    def is_trade_day(self, d: date) -> bool:
        trade_days = self._load()
        if not trade_days:
            # fallback: weekday-based check
            return d.weekday() < 5
        return d.strftime("%Y-%m-%d") in trade_days

    def refresh(self) -> None:
        """Force reload on next query (e.g., after nightly calendar update)."""
        self._trade_days = None


class WeekdayFallbackCalendar:
    """当 provider_uri 不可用时的保底实现."""

    def is_trade_day(self, d: date) -> bool:
        return d.weekday() < 5


def make_calendar(provider_uri: Optional[str] = None):
    """Factory — 若 provider_uri 有效则用 QlibCalendar, 否则 weekday fallback."""
    if provider_uri and (Path(provider_uri) / "calendars" / "day.txt").exists():
        return QlibCalendar(provider_uri)
    return WeekdayFallbackCalendar()
