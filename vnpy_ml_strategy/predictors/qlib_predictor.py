"""QlibPredictor — subprocess wrapper around ``qlib_strategy_core.cli.run_inference``.

vnpy 主进程 (Python 3.13) 调本类的 ``predict()`` 即启一个 **一次性子进程**
跑研究机 Python 3.11 + vendored qlib, 读回三件套文件, 无任何 qlib / mlflow /
lightgbm 的 import 发生在 vnpy 进程里.

失败/超时语义 (与 core diagnostics.json 对齐):
* ``status=ok`` — 正常完成, 主进程拿去下单
* ``status=empty`` — 子进程跑完但预测为空, 不下单
* ``status=failed`` — 子进程 Python 层抛异常, 详情在 diagnostics.error_*
* ``TimeoutExpired`` — 主进程 kill 子进程, ``predict()`` 抛 ``InferenceTimeout``
* 无 diagnostics.json — 视为 ``unknown`` 失败 (OOM / 段错误 / 进程被 kill)

所有场景 ``predict()`` 都返回一个 dict (或抛对应异常), 让 MLStrategyTemplate
的 run_daily_pipeline 做分支判断.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from ..base import DIAGNOSTICS_SCHEMA_MAJOR


class InferenceTimeout(RuntimeError):
    """Subprocess exceeded ``timeout_s``."""


class InferenceSchemaError(RuntimeError):
    """diagnostics.json schema version doesn't match expected major."""


class QlibPredictor:
    """Phase 2.2 — subprocess wrapper, 无任何 qlib import 在 vnpy 进程里."""

    # 主进程应在此路径之上 insert PYTHONPATH, 使子进程能 import qlib_strategy_core
    # 默认从 vnpy_strategy_dev/vendor/qlib_strategy_core/ 取
    DEFAULT_CORE_PATH = Path(__file__).resolve().parents[2] / "vendor" / "qlib_strategy_core"

    def __init__(self, core_path: Optional[str] = None, install_legacy_path: bool = True):
        """
        Parameters
        ----------
        core_path : str, optional
            qlib_strategy_core 源码根目录. 默认指向本 vnpy_strategy_dev 的 vendor/
            子模块. 子进程的 PYTHONPATH 会被设置为这个路径.
        install_legacy_path : bool
            是否让子进程启用 legacy MetaPathFinder. 对于 Phase 1 之前的旧 bundle
            (module_path=factor_factory.*) 需要设 True; 新 bundle 设 False 更干净.
            默认 True 以最大兼容.
        """
        self.core_path = Path(core_path) if core_path else self.DEFAULT_CORE_PATH
        self.install_legacy_path = install_legacy_path

    # ------------------------------------------------------------------
    # BasePredictor 接口
    # ------------------------------------------------------------------

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
        filter_parquet_path: Optional[str] = None,
        timeout_s: int = 180,
    ) -> Dict[str, Any]:
        """Invoke run_inference subprocess.

        Parameters
        ----------
        filter_parquet_path : str, optional
            Phase 4 v2: 推理唯一输入之一. 按 live_end 指向
            ``snapshots/filtered/csi300_filtered_{YYYYMMDD}.parquet``, 覆盖
            bundle task.json 固化的训练时点过滤路径. 若 None, handler kwargs
            保持 task.json 默认. 生产必设.
        """
        out_dir = Path(output_root) / strategy_name / live_end.strftime("%Y%m%d")
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            inference_python,
            "-m", "qlib_strategy_core.cli.run_inference",
            "--bundle-dir", str(bundle_dir),
            "--live-end", live_end.strftime("%Y-%m-%d"),
            "--lookback", str(lookback_days),
            "--out-dir", str(out_dir),
            "--strategy", strategy_name,
            "--provider-uri", provider_uri,
        ]
        if baseline_path:
            cmd += ["--baseline", str(baseline_path)]
        if filter_parquet_path:
            cmd += ["--filter-parquet", str(filter_parquet_path)]
        if self.install_legacy_path:
            cmd += ["--install-legacy-path"]

        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.core_path) + (os.pathsep + existing if existing else "")
        # 让子进程 stdout/stderr 用 UTF-8, 避免 Windows GBK 和 qlib 中文输出打架
        env.setdefault("PYTHONIOENCODING", "utf-8")

        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",  # 解码失败不抛异常, 用 \ufffd 替代
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess killed — diagnostics.json may be absent or partial
            raise InferenceTimeout(
                f"inference subprocess exceeded {timeout_s}s for strategy={strategy_name}"
            ) from exc

        diag_path = out_dir / "diagnostics.json"
        if not diag_path.exists():
            # subprocess exited but didn't write sentinel — treat as failure
            return {
                "pred_df": None,
                "metrics": {},
                "diagnostics": {
                    "schema_version": DIAGNOSTICS_SCHEMA_MAJOR,
                    "strategy": strategy_name,
                    "status": "failed",
                    "exit_code": result.returncode,
                    "error_type": "NoDiagnostics",
                    "error_message": "subprocess did not produce diagnostics.json",
                    "stderr_tail": result.stderr[-1000:] if result.stderr else "",
                },
            }

        diag = json.loads(diag_path.read_text(encoding="utf-8"))

        # schema version gate
        if diag.get("schema_version", 0) != DIAGNOSTICS_SCHEMA_MAJOR:
            raise InferenceSchemaError(
                f"diagnostics.schema_version={diag.get('schema_version')} "
                f"expected {DIAGNOSTICS_SCHEMA_MAJOR}"
            )

        # Load metrics + predictions only if status != failed (save I/O on failure path)
        metrics: Dict[str, Any] = {}
        pred_df: Optional[pd.DataFrame] = None

        metrics_path = out_dir / "metrics.json"
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))

        pred_path = out_dir / "predictions.parquet"
        if pred_path.exists():
            pred_df = pd.read_parquet(pred_path)

        return {
            "pred_df": pred_df,
            "metrics": metrics,
            "diagnostics": diag,
        }
