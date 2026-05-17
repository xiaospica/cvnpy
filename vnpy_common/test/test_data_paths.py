"""Tests for centralized vnpy data-root path resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from vnpy_common.data_paths import ensure_vnpy_data_env, state_dir, vnpy_data_root


def test_vnpy_data_root_requires_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VNPY_DATA_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="VNPY_DATA_ROOT"):
        vnpy_data_root()


def test_vnpy_data_root_rejects_missing_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing_root"
    monkeypatch.setenv("VNPY_DATA_ROOT", str(missing))

    with pytest.raises(FileNotFoundError, match="VNPY_DATA_ROOT"):
        vnpy_data_root()


def test_vnpy_data_root_uses_existing_env_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VNPY_DATA_ROOT", str(tmp_path))

    assert vnpy_data_root() == tmp_path
    assert state_dir() == tmp_path / "state"
    assert ensure_vnpy_data_env() == tmp_path
