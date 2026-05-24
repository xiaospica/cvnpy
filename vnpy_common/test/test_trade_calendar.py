from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vnpy_common.trade_calendar import (
    FileTradeCalendar,
    StaleCalendarError,
    ashare_calendar_path,
    make_calendar,
    publish_ashare_trade_day,
    publish_ashare_trade_days,
)


def test_publish_ashare_trade_day_seeds_from_qlib_without_rewriting_qlib(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    qlib_calendar = tmp_path / "qlib_data_bin" / "calendars" / "day.txt"
    qlib_calendar.parent.mkdir(parents=True)
    qlib_calendar.write_text("2026-05-22\n", encoding="utf-8")

    target = publish_ashare_trade_day("20260525")

    assert target == ashare_calendar_path()
    assert target.read_text(encoding="utf-8").splitlines() == [
        "2026-05-22",
        "2026-05-25",
    ]
    assert qlib_calendar.read_text(encoding="utf-8") == "2026-05-22\n"
    assert make_calendar().is_trade_day(date(2026, 5, 25)) is True


def test_publish_ashare_trade_days_works_without_qlib_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))

    target = publish_ashare_trade_days(["20260520", "2026-05-22"])

    assert target == ashare_calendar_path()
    assert target.read_text(encoding="utf-8").splitlines() == [
        "2026-05-20",
        "2026-05-22",
    ]


def test_file_trade_calendar_raises_when_weekday_query_exceeds_tail(tmp_path: Path) -> None:
    path = tmp_path / "day.txt"
    path.write_text("2026-05-22\n", encoding="utf-8")
    calendar = FileTradeCalendar(path)

    with pytest.raises(StaleCalendarError):
        calendar.is_trade_day(date(2026, 5, 25))

    assert calendar.is_trade_day(date(2026, 5, 23)) is False


def test_make_calendar_falls_back_to_qlib_when_shared_calendar_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    qlib_calendar = tmp_path / "qlib_data_bin" / "calendars" / "day.txt"
    qlib_calendar.parent.mkdir(parents=True)
    qlib_calendar.write_text("2026-05-22\n", encoding="utf-8")

    calendar = make_calendar()

    assert isinstance(calendar, FileTradeCalendar)
    assert calendar.is_trade_day(date(2026, 5, 22)) is True


def test_make_calendar_falls_back_to_qlib_when_default_path_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))
    qlib_calendar = tmp_path / "qlib_data_bin" / "calendars" / "day.txt"
    qlib_calendar.parent.mkdir(parents=True)
    qlib_calendar.write_text("2026-05-22\n", encoding="utf-8")

    calendar = make_calendar(tmp_path / "state" / "trade_calendars" / "ashare_day.txt")

    assert isinstance(calendar, FileTradeCalendar)
    assert calendar.is_trade_day(date(2026, 5, 22)) is True
