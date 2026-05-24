"""Shared A-share trading calendar utilities.

The runtime calendar is intentionally separate from qlib provider publishing.
Non-ML consumers such as QMT_SIM replay only need to know which A-share days
exist, while ``qlib_data_bin/calendars/day.txt`` must remain coupled to the
full qlib feature dump.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Set

from .data_paths import data_path


ASHARE_CALENDAR_ENV = "VNPY_ASHARE_CALENDAR_PATH"


class StaleCalendarError(RuntimeError):
    """Raised when a weekday query is newer than the calendar's known tail."""


class FileTradeCalendar:
    """Read trading days from a newline-delimited ``YYYY-MM-DD`` file."""

    def __init__(self, calendar_path: str | Path):
        self.calendar_path = Path(os.path.expandvars(str(calendar_path))).expanduser()
        self._trade_days: Optional[Set[str]] = None
        self._max_known: Optional[str] = None

    def _load(self) -> Set[str]:
        if self._trade_days is not None:
            return self._trade_days
        if not self.calendar_path.exists():
            self._trade_days = set()
            self._max_known = None
            return self._trade_days

        self._trade_days = {
            line.strip()
            for line in self.calendar_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        self._max_known = max(self._trade_days) if self._trade_days else None
        return self._trade_days

    def refresh(self) -> None:
        """Force a reload on the next query."""
        self._trade_days = None
        self._max_known = None

    def is_trade_day(self, day: date) -> bool:
        """Return whether ``day`` is an A-share trading day."""
        trade_days = self._load()
        if not trade_days:
            return day.weekday() < 5
        if day.weekday() >= 5:
            return False

        day_text = day.isoformat()
        if self._max_known is not None and day_text > self._max_known:
            raise StaleCalendarError(
                f"calendar tail {self._max_known} < query day {day_text}; "
                "update the shared A-share calendar before replay settlement"
            )
        return day_text in trade_days

    def prev_trade_day(self, day: date, max_lookback: int = 14) -> Optional[date]:
        """Return the nearest trading day before ``day``."""
        trade_days = self._load()
        cursor = day - timedelta(days=1)
        for _ in range(max_lookback):
            if trade_days:
                if cursor.isoformat() in trade_days:
                    return cursor
            elif cursor.weekday() < 5:
                return cursor
            cursor -= timedelta(days=1)
        return None


class WeekdayFallbackCalendar:
    """Fallback calendar used only when no file-backed calendar exists."""

    def is_trade_day(self, day: date) -> bool:
        return day.weekday() < 5

    def prev_trade_day(self, day: date, max_lookback: int = 14) -> Optional[date]:
        cursor = day - timedelta(days=1)
        for _ in range(max_lookback):
            if cursor.weekday() < 5:
                return cursor
            cursor -= timedelta(days=1)
        return None


def ashare_calendar_path() -> Path:
    """Return the shared A-share trading calendar path."""
    explicit = os.getenv(ASHARE_CALENDAR_ENV, "").strip()
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser()
    return data_path("state", "trade_calendars", "ashare_day.txt")


def qlib_calendar_path(provider_uri: str | Path | None = None) -> Path:
    """Return the qlib provider calendar path used only as a compatibility seed."""
    if provider_uri is None:
        provider_uri = os.getenv("ML_QLIB_DIR", "").strip() or data_path("qlib_data_bin")
    return Path(os.path.expandvars(str(provider_uri))).expanduser() / "calendars" / "day.txt"


def normalize_calendar_path(value: object) -> Path:
    """Normalize a calendar file path or a qlib-style provider root to ``day.txt``."""
    raw = os.path.expandvars(str(value or "")).strip()
    if not raw:
        return ashare_calendar_path()
    path = Path(raw).expanduser()
    if path.name == "day.txt" or path.suffix.lower() == ".txt":
        return path
    return path / "calendars" / "day.txt"


def make_calendar(calendar_path: str | Path | None = None):
    """Build a file-backed calendar, falling back to qlib or weekdays."""
    candidates: list[Path] = []
    if calendar_path:
        candidates.append(normalize_calendar_path(calendar_path))
        qlib_path = qlib_calendar_path()
        if qlib_path not in candidates:
            candidates.append(qlib_path)
    else:
        candidates.append(ashare_calendar_path())
        candidates.append(qlib_calendar_path())

    for candidate in candidates:
        if candidate.exists():
            return FileTradeCalendar(candidate)
    return WeekdayFallbackCalendar()


def publish_ashare_trade_day(
    trade_day: str | date | datetime,
    *,
    calendar_path: str | Path | None = None,
    seed_paths: Iterable[str | Path] | None = None,
) -> Path:
    """Ensure the shared A-share calendar contains ``trade_day``.

    Parameters
    ----------
    trade_day:
        Confirmed trading day from the data fetch pipeline.
    calendar_path:
        Optional destination file. Defaults to
        ``<VNPY_DATA_ROOT>/state/trade_calendars/ashare_day.txt``.
    seed_paths:
        Optional existing calendars to merge into the destination. When omitted,
        the current qlib provider calendar is used as a seed if it exists.
    """
    return publish_ashare_trade_days(
        [trade_day],
        calendar_path=calendar_path,
        seed_paths=seed_paths,
    )


def publish_ashare_trade_days(
    trade_days: Iterable[str | date | datetime],
    *,
    calendar_path: str | Path | None = None,
    seed_paths: Iterable[str | Path] | None = None,
) -> Path:
    """Ensure the shared A-share calendar contains all ``trade_days``.

    Parameters
    ----------
    trade_days:
        Confirmed trading days from a fetch result, snapshot, or other trusted
        runtime data source.
    calendar_path:
        Optional destination file. Defaults to
        ``<VNPY_DATA_ROOT>/state/trade_calendars/ashare_day.txt``.
    seed_paths:
        Optional existing calendars to merge into the destination. When omitted,
        the current qlib provider calendar is used as a seed if it exists.
    """
    target = Path(calendar_path).expanduser() if calendar_path else ashare_calendar_path()
    seeds = [Path(p).expanduser() for p in seed_paths] if seed_paths is not None else [qlib_calendar_path()]

    days: set[str] = set()
    for path in [target, *seeds]:
        if not path.exists():
            continue
        days.update(_read_calendar_days(path))

    new_days = {_coerce_day(day).isoformat() for day in trade_days}
    if not new_days:
        raise ValueError("trade_days must not be empty")
    days.update(new_days)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp")
    tmp_path.write_text("\n".join(sorted(days)) + "\n", encoding="utf-8")
    os.replace(tmp_path, target)
    return target


def _read_calendar_days(path: Path) -> set[str]:
    days: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            days.add(_coerce_day(text).isoformat())
        except ValueError:
            continue
    return days


def _coerce_day(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return datetime.fromisoformat(text).date()
