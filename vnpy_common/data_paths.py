"""Centralized runtime data paths for vnpy_strategy_dev.

The normal deployment surface is a single environment variable:
``VNPY_DATA_ROOT``. Other path variables are treated as advanced explicit
overrides only; defaults are always derived from the root.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


DEFAULT_VNPY_DATA_ROOT = Path("D:/vnpy_data")
LEGACY_PATH_ENV_VARS = {
    "QS_DATA_ROOT",
    "ML_OUTPUT_ROOT",
    "VNPY_MODEL_ROOT",
    "LOG_ROOT",
    "BACKUP_ROOT",
    "ML_SNAPSHOT_DIR",
    "VNPY_QMT_SIM_TRADING_STATE",
}


def vnpy_data_root() -> Path:
    return Path(os.getenv("VNPY_DATA_ROOT") or DEFAULT_VNPY_DATA_ROOT).expanduser()


def ensure_vnpy_data_env() -> Path:
    """Ensure ${VNPY_DATA_ROOT} expands in yaml/config templates."""
    root = vnpy_data_root()
    os.environ.setdefault("VNPY_DATA_ROOT", str(root))
    return root


def data_path(*parts: str) -> Path:
    return vnpy_data_root().joinpath(*parts)


def stock_list_path() -> Path:
    explicit = os.getenv("TUSHARE_STOCK_LIST_PATH", "").strip()
    return Path(explicit).expanduser() if explicit else data_path("stock_data", "stock_list.parquet")


def ensure_stock_list_env() -> Path:
    path = stock_list_path()
    if not os.getenv("TUSHARE_STOCK_LIST_PATH", "").strip():
        os.environ["TUSHARE_STOCK_LIST_PATH"] = str(path)
    return path

def config_dir() -> Path:
    return data_path("config")


def state_dir() -> Path:
    return data_path("state")


def strategy_equity_journal_db_path() -> Path:
    return state_dir() / "strategy_equity_journal.db"


def event_journal_db_path() -> Path:
    return state_dir() / "event_journal.db"


def sim_state_dir() -> Path:
    explicit = os.getenv("VNPY_QMT_SIM_TRADING_STATE")
    return Path(explicit).expanduser() if explicit else state_dir()


def snapshots_dir() -> Path:
    explicit = os.getenv("ML_SNAPSHOT_DIR")
    return Path(explicit).expanduser() if explicit else data_path("snapshots")


def merged_snapshots_dir() -> Path:
    return snapshots_dir() / "merged"


def filtered_snapshots_dir() -> Path:
    return snapshots_dir() / "filtered"


def ml_output_root() -> Path:
    explicit = os.getenv("ML_OUTPUT_ROOT")
    return Path(explicit).expanduser() if explicit else data_path("ml_output")


def models_root() -> Path:
    explicit = os.getenv("VNPY_MODEL_ROOT")
    return Path(explicit).expanduser() if explicit else data_path("models")


def logs_root() -> Path:
    explicit = os.getenv("LOG_ROOT")
    return Path(explicit).expanduser() if explicit else data_path("logs")


def backups_root() -> Path:
    explicit = os.getenv("BACKUP_ROOT")
    return Path(explicit).expanduser() if explicit else data_path("backups")


def legacy_path_env_warnings(env_names: Iterable[str] = LEGACY_PATH_ENV_VARS) -> list[str]:
    return sorted(name for name in env_names if os.getenv(name))
