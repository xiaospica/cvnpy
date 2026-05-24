from __future__ import annotations

import importlib
import time

import pytest


def test_mysql_connect_args_defaults_and_overrides(monkeypatch) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", r"D:\vnpy_data")
    mod = importlib.import_module("run_signal_dual_track")

    assert mod._mysql_connect_args({}) == {
        "connect_timeout": 10,
        "read_timeout": 10,
        "write_timeout": 10,
    }
    assert mod._mysql_connect_args(
        {
            "connect_timeout": "3",
            "read_timeout": 0,
            "write_timeout": "bad",
        }
    ) == {
        "connect_timeout": 3,
        "read_timeout": 10,
        "write_timeout": 10,
    }


def test_run_with_timeout_raises_quickly(monkeypatch) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", r"D:\vnpy_data")
    mod = importlib.import_module("run_signal_dual_track")

    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="unit test call timed out"):
        mod._run_with_timeout(
            lambda: time.sleep(0.2),
            timeout=0.01,
            label="unit test call",
        )

    assert time.perf_counter() - started < 0.15


def test_cleanup_mysql_timeout_is_not_swallowed(monkeypatch) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", r"D:\vnpy_data")
    mod = importlib.import_module("run_signal_dual_track")

    def _raise_timeout(*args, **kwargs):
        raise TimeoutError("mysql timeout")

    monkeypatch.setattr(mod, "_mysql_engine", _raise_timeout)

    with pytest.raises(TimeoutError, match="mysql timeout"):
        mod._delete_strategy_application_rows({"mysql": {}}, ["strategy"])
    with pytest.raises(TimeoutError, match="mysql timeout"):
        mod._delete_shadow_mysql_rows({"mysql": {}}, "strategy_shadow")
