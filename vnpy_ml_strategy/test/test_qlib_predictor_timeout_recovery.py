from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from vnpy_ml_strategy.predictors import qlib_predictor as qp


def _write_ok_outputs(out_dir: Path, strategy_name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnostics.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "strategy": strategy_name,
                "live_end": "2026-05-07",
                "status": "ok",
                "exit_code": 0,
                "rows": 2,
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "metrics.json").write_text(
        json.dumps({"n_predictions": 2}),
        encoding="utf-8",
    )
    pd.DataFrame(
        {"score": [0.1, 0.2]},
        index=pd.MultiIndex.from_tuples(
            [
                (pd.Timestamp("2026-05-07"), "000001.SZ"),
                (pd.Timestamp("2026-05-07"), "000002.SZ"),
            ],
            names=["datetime", "instrument"],
        ),
    ).to_parquet(out_dir / "predictions.parquet")


def test_predict_recovers_outputs_after_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    predictor = qp.QlibPredictor(core_path=str(tmp_path))
    out_dir = tmp_path / "out" / "demo_strategy" / "20260507"

    def fake_run(*args, **kwargs):
        _write_ok_outputs(out_dir, "demo_strategy")
        raise qp.InferenceTimeout("simulated timeout after outputs")

    monkeypatch.setattr(qp, "_run_subprocess_monitored", fake_run)

    result = predictor.predict(
        bundle_dir=str(tmp_path / "bundle"),
        live_end=date(2026, 5, 7),
        lookback_days=60,
        strategy_name="demo_strategy",
        inference_python="python",
        output_root=str(tmp_path / "out"),
        provider_uri=str(tmp_path / "provider"),
        timeout_s=1,
        memory_limit_mb=0,
    )

    assert result["diagnostics"]["status"] == "ok"
    assert result["metrics"]["n_predictions"] == 2
    assert result["pred_df"] is not None
    assert len(result["pred_df"]) == 2


def test_predict_timeout_does_not_reuse_stale_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    predictor = qp.QlibPredictor(core_path=str(tmp_path))
    out_dir = tmp_path / "out" / "demo_strategy" / "20260507"
    _write_ok_outputs(out_dir, "demo_strategy")

    def fake_run(*args, **kwargs):
        raise qp.InferenceTimeout("simulated hard timeout before new outputs")

    monkeypatch.setattr(qp, "_run_subprocess_monitored", fake_run)

    with pytest.raises(qp.InferenceTimeout):
        predictor.predict(
            bundle_dir=str(tmp_path / "bundle"),
            live_end=date(2026, 5, 7),
            lookback_days=60,
            strategy_name="demo_strategy",
            inference_python="python",
            output_root=str(tmp_path / "out"),
            provider_uri=str(tmp_path / "provider"),
            timeout_s=1,
            memory_limit_mb=0,
        )
