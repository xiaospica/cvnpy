"""Tests for replay acceptance artifact capture."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from vnpy_qmt_sim.replay.acceptance import copy_and_export_sqlite


def test_copy_and_export_sqlite_uses_backup_for_wal(tmp_path: Path) -> None:
    """SQLite backup must include rows that still live in the WAL file."""
    source = tmp_path / "wal_source.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE sim_trades (id INTEGER PRIMARY KEY, vt_symbol TEXT, volume REAL)")
        conn.execute(
            "INSERT INTO sim_trades(vt_symbol, volume) VALUES (?, ?)",
            ("000001.SZSE", 100.0),
        )
        conn.commit()
        assert source.with_name(f"{source.name}-wal").exists()

        manifest: dict[str, object] = {
            "warnings": [],
            "copied_files": [],
            "exports": [],
        }
        copy_and_export_sqlite(source, tmp_path / "capture", [], manifest)
    finally:
        conn.close()

    export_path = (
        tmp_path
        / "capture"
        / "sqlite_exports"
        / source.stem
        / "sim_trades.jsonl"
    )
    rows = [
        json.loads(line)
        for line in export_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows == [{"id": 1, "vt_symbol": "000001.SZSE", "volume": 100.0}]
    assert manifest["warnings"] == []
