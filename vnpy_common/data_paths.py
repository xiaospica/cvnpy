"""Centralized runtime data paths for vnpy_strategy_dev.

The normal deployment surface is a single environment variable:
``VNPY_DATA_ROOT``. Other path variables are treated as advanced explicit
overrides only; defaults are always derived from the root. Runtime code must
fail fast when the root is missing or invalid; there is deliberately no
hard-coded data-root fallback.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


VNPY_DATA_ROOT_ENV = "VNPY_DATA_ROOT"
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
    raw = os.getenv(VNPY_DATA_ROOT_ENV, "").strip().strip('"').strip("'")
    if not raw:
        raise RuntimeError(
            "VNPY_DATA_ROOT 未设置。请在 .env/.env.production 或服务环境中显式配置 "
            "VNPY_DATA_ROOT；运行时不再提供默认数据目录。"
        )
    root = Path(os.path.expandvars(raw)).expanduser()
    if not root.exists():
        raise FileNotFoundError(
            f"VNPY_DATA_ROOT 指向的目录不存在: {root}. "
            "请先创建/迁移数据目录，或修正 VNPY_DATA_ROOT。"
        )
    if not root.is_dir():
        raise NotADirectoryError(f"VNPY_DATA_ROOT 不是目录: {root}")
    return root


def ensure_vnpy_data_env() -> Path:
    """Ensure ${VNPY_DATA_ROOT} expands in yaml/config templates."""
    root = vnpy_data_root()
    os.environ[VNPY_DATA_ROOT_ENV] = str(root)
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
