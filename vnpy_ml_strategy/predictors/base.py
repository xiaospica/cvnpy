"""Predictor Protocol — 所有预测实现必须符合此接口.

按 vnpy 架构师方案, predictor 是可插拔的. 当前实现 ``QlibPredictor`` 走子进程,
未来如要切长驻 daemon (C 方案) 只需换一个实现, ``MLEngine`` 不用动.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class BasePredictor(Protocol):
    """All predictors must return the same dict shape."""

    def predict(
        self,
        *,
        bundle_dir: str,
        live_end: date,
        lookback_days: int,
        strategy_name: str,
        inference_python: str,
        output_root: str,
        provider_uri: str,
        baseline_path: Optional[str] = None,
        timeout_s: int = 180,
    ) -> Dict[str, Any]:
        """Run one-shot inference and return a dict:

            {
                "pred_df": pd.DataFrame,        # MultiIndex (datetime, instrument)
                "metrics": dict,                # from metrics.json
                "diagnostics": dict,            # from diagnostics.json (status/exit_code/duration_ms/...)
            }

        Raises on timeout or missing sentinel.
        """
        ...
