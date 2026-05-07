from __future__ import annotations

import subprocess
from pathlib import Path

from vnpy_ml_strategy.services.ic_backfill import IcBackfillService


def test_ic_backfill_decodes_non_utf8_subprocess_output(monkeypatch, tmp_path: Path) -> None:
    summary = b'{"scanned": 5, "computed": 3, "errors": 0, "duration_ms": 12}\n'
    gbk_log = "开始回填IC...\n".encode("gb18030")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=kwargs.get("args", args[0] if args else []),
            returncode=0,
            stdout=gbk_log + summary,
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    svc = IcBackfillService(
        strategy_name="demo",
        output_root=str(tmp_path / "out"),
        provider_uri=str(tmp_path / "qlib"),
        inference_python="F:/Program_Home/vnpy/python.exe",
    )
    result = svc.run_sync()

    assert result.success is True
    assert result.scanned == 5
    assert result.computed == 3
    assert result.errors == 0
