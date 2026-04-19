"""Phase 2.2 — subprocess predictor 失败/超时语义 (不调真子进程, mock subprocess.run)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from vnpy_ml_strategy.predictors.qlib_predictor import (
    InferenceSchemaError,
    InferenceTimeout,
    QlibPredictor,
)


class _FakeCompleted:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def _write_diag(out_dir: Path, **kwargs):
    base = {"schema_version": 1, "status": "ok", "exit_code": 0, "rows": 10}
    base.update(kwargs)
    (out_dir / "diagnostics.json").write_text(json.dumps(base))


def test_predict_ok_reads_three_files(tmp_path):
    day_dir = tmp_path / "demo" / "20260420"
    day_dir.mkdir(parents=True)

    # pre-stage subprocess output
    import pandas as pd
    df = pd.DataFrame({"score": [0.1, 0.2]},
                       index=pd.MultiIndex.from_tuples(
                           [(pd.Timestamp("2026-04-20"), "000001"),
                            (pd.Timestamp("2026-04-20"), "000002")],
                           names=["datetime", "instrument"]))
    df.to_parquet(day_dir / "predictions.parquet")
    (day_dir / "metrics.json").write_text(json.dumps({"ic": 0.05}))
    _write_diag(day_dir, rows=2)

    with patch.object(subprocess, "run", return_value=_FakeCompleted()):
        p = QlibPredictor()
        result = p.predict(
            bundle_dir=str(tmp_path / "fake_bundle"),
            live_end=date(2026, 4, 20),
            lookback_days=60,
            strategy_name="demo",
            inference_python="python",
            output_root=str(tmp_path),
            provider_uri="x",
            timeout_s=10,
        )

    assert result["diagnostics"]["status"] == "ok"
    assert result["diagnostics"]["rows"] == 2
    assert result["metrics"] == {"ic": 0.05}
    assert result["pred_df"].shape == (2, 1)


def test_predict_timeout_raises(tmp_path):
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=5)
    with patch.object(subprocess, "run", side_effect=_raise):
        p = QlibPredictor()
        with pytest.raises(InferenceTimeout):
            p.predict(
                bundle_dir=str(tmp_path / "x"),
                live_end=date(2026, 4, 20),
                lookback_days=60,
                strategy_name="demo",
                inference_python="python",
                output_root=str(tmp_path),
                provider_uri="x",
                timeout_s=5,
            )


def test_predict_no_diagnostics_returns_failed(tmp_path):
    """subprocess ran but didn't produce diagnostics.json — treat as failed."""
    with patch.object(subprocess, "run", return_value=_FakeCompleted(returncode=-9, stderr="killed")):
        p = QlibPredictor()
        result = p.predict(
            bundle_dir=str(tmp_path / "fake"),
            live_end=date(2026, 4, 20),
            lookback_days=60,
            strategy_name="demo",
            inference_python="python",
            output_root=str(tmp_path),
            provider_uri="x",
        )
    assert result["diagnostics"]["status"] == "failed"
    assert result["diagnostics"]["error_type"] == "NoDiagnostics"
    assert result["pred_df"] is None


def test_predict_schema_mismatch_raises(tmp_path):
    day_dir = tmp_path / "demo" / "20260420"
    day_dir.mkdir(parents=True)
    _write_diag(day_dir, schema_version=99)

    with patch.object(subprocess, "run", return_value=_FakeCompleted()):
        p = QlibPredictor()
        with pytest.raises(InferenceSchemaError):
            p.predict(
                bundle_dir=str(tmp_path / "fake"),
                live_end=date(2026, 4, 20),
                lookback_days=60,
                strategy_name="demo",
                inference_python="python",
                output_root=str(tmp_path),
                provider_uri="x",
            )
