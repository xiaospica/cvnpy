"""QlibPredictor — subprocess wrapper around ``qlib_strategy_core.cli.run_inference``.

vnpy 主进程 (Python 3.13) 调本类的 ``predict()`` 即启一个 **一次性子进程**
跑研究机 Python 3.11 + vendored qlib, 读回三件套文件, 无任何 qlib / mlflow /
lightgbm 的 import 发生在 vnpy 进程里.

失败/超时语义 (与 core diagnostics.json 对齐):
* ``status=ok`` — 正常完成, 主进程拿去下单
* ``status=empty`` — 子进程跑完但预测为空, 不下单
* ``status=failed`` — 子进程 Python 层抛异常, 详情在 diagnostics.error_*
* ``TimeoutExpired`` — 主进程 kill 子进程, ``predict()`` 抛 ``InferenceTimeout``
* OOM (RSS 超阈值) — 主进程 kill 子进程整树, 抛 ``InferenceOOM`` (P1-5)
* 无 diagnostics.json — 视为 ``unknown`` 失败 (OOM / 段错误 / 进程被 kill)

所有场景 ``predict()`` 都返回一个 dict (或抛对应异常), 让 MLStrategyTemplate
的 run_daily_pipeline 做分支判断.

P1-5 OOM/超时监控 (本模块):
  - 单策略推理 RSS 峰值 ~5 GB (csi300) / ~8 GB (zz500/all).
  - 用 psutil.Process.memory_info().rss 每 2s 轮询;
    超 ``memory_limit_mb`` 阈值 → terminate 进程树 → raise InferenceOOM.
  - 默认 memory_limit_mb=12288 (12 GB), 给 zz500 留余量;
    csi300 跑稳定后可降到 8192.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import psutil

from ..base import DIAGNOSTICS_SCHEMA_MAJOR


class InferenceTimeout(RuntimeError):
    """Subprocess exceeded ``timeout_s``."""


class InferenceOOM(RuntimeError):
    """Subprocess RSS exceeded ``memory_limit_mb``. Killed by main process."""


class InferenceSchemaError(RuntimeError):
    """diagnostics.json schema version doesn't match expected major."""


# P1-5: 子进程监控轮询周期 (秒). 太短增加 psutil 开销, 太长 OOM 检测滞后.
# 2s 在 8 GB 阈值下意味着: 子进程从 7 GB → 9 GB 至多 2s 内被 kill, 不至于
# 把 16 GB 机器拖崩.
_MONITOR_POLL_INTERVAL_SECONDS = 2.0


def _get_total_rss_mb(proc: psutil.Process) -> float:
    """递归累计 proc 自己 + 所有子孙进程的 RSS. 返回 MB.

    qlib 子进程内部 multiprocessing.spawn worker 的内存也算进来 — 总内存
    才是 OS 看到的真实占用, 单进程 rss 会低估.
    """
    try:
        total_bytes = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total_bytes += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0
    return total_bytes / (1024 * 1024)


def _kill_process_tree(pid: int, timeout: float = 5.0) -> None:
    """Best-effort kill of pid + all children. Returns when all dead or timeout."""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = proc.children(recursive=True) + [proc]
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _run_subprocess_monitored(
    cmd: list,
    env: dict,
    *,
    timeout_s: int,
    memory_limit_mb: int,
    label: str,
) -> subprocess.CompletedProcess:
    """[P1-5] Popen + psutil 轮询, 监控 RSS + 超时, 触发即 kill 进程树.

    Args:
        cmd: subprocess 启动命令.
        env: 子进程环境变量.
        timeout_s: 总超时 (秒).
        memory_limit_mb: RSS 阈值 (整树, MB). 0 表示禁用 RSS 监控.
        label: 日志用标签 (e.g. "csi300_lgb T=2026-04-30").

    Returns:
        subprocess.CompletedProcess (returncode / stdout / stderr).

    Raises:
        InferenceTimeout: 总耗时 > timeout_s.
        InferenceOOM: RSS 整树 > memory_limit_mb.
    """
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        ps_proc = psutil.Process(proc.pid)
    except psutil.NoSuchProcess:
        # 进程刚启动就退了 → 走 communicate 收集输出
        out, err = proc.communicate(timeout=5)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    start_ts = time.monotonic()
    peak_rss_mb = 0.0

    while True:
        # 若进程已退, 跳出收集输出
        if proc.poll() is not None:
            break

        # 总耗时检查
        elapsed = time.monotonic() - start_ts
        if elapsed > timeout_s:
            _kill_process_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            raise InferenceTimeout(
                f"[{label}] subprocess exceeded {timeout_s}s "
                f"(peak_rss={peak_rss_mb:.0f}MB); killed."
            )

        # RSS 检查 (memory_limit_mb=0 跳过)
        if memory_limit_mb > 0:
            rss_mb = _get_total_rss_mb(ps_proc)
            if rss_mb > peak_rss_mb:
                peak_rss_mb = rss_mb
            if rss_mb > memory_limit_mb:
                _kill_process_tree(proc.pid)
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                raise InferenceOOM(
                    f"[{label}] subprocess RSS={rss_mb:.0f}MB > "
                    f"limit={memory_limit_mb}MB after {elapsed:.0f}s; killed. "
                    f"stderr_tail={(err or '')[-500:]!r}"
                )

        time.sleep(_MONITOR_POLL_INTERVAL_SECONDS)

    # 进程已退, 收集剩余输出 (PIPE 可能仍有 buffer)
    try:
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        out, err = "", ""
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
        memory_limit_mb: int = 12288,
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

        # P1-5: monitored runner (RSS + 超时双监控) 替代 subprocess.run.
        # OOM / timeout 抛 InferenceOOM / InferenceTimeout, 由调用方分支处理.
        result = _run_subprocess_monitored(
            cmd,
            env,
            timeout_s=timeout_s,
            memory_limit_mb=memory_limit_mb,
            label=f"{strategy_name} T={live_end.isoformat()}",
        )

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

    def run_range(
        self,
        bundle_dir: str,
        range_start,
        range_end,
        lookback_days: int,
        strategy_name: str,
        inference_python: str,
        output_root: str,
        provider_uri: str,
        baseline_path: Optional[str] = None,
        timeout_s: int = 3600,
        filter_parquet_path: Optional[str] = None,
        memory_limit_mb: int = 12288,
    ) -> Dict[str, Any]:
        """Phase 4 加速回放：批量推理子进程，一次性产出多日 predictions + diagnostics。

        Parameters
        ----------
        range_start, range_end : datetime.date | str
            回放起止日（包含两端）
        output_root : str
            子进程会按 ``{output_root}/{strategy_name}/{yyyymmdd}/`` 建子目录写文件
        timeout_s : int
            默认 3600s（1 小时）。回放 80 个交易日典型耗时 ~10 分钟，给余量

        Returns
        -------
        dict: {n_days_total, n_days_with_data, exit_code, stderr_tail, returncode}
        """
        from datetime import date as _date
        if isinstance(range_start, _date):
            range_start_str = range_start.strftime("%Y-%m-%d")
        else:
            range_start_str = str(range_start)
        if isinstance(range_end, _date):
            range_end_str = range_end.strftime("%Y-%m-%d")
        else:
            range_end_str = str(range_end)

        # batch 模式 out-dir 是 strategy 父目录（子进程内部按日建子目录）
        strategy_out_root = Path(output_root) / strategy_name
        strategy_out_root.mkdir(parents=True, exist_ok=True)

        cmd = [
            inference_python,
            "-m", "qlib_strategy_core.cli.run_inference",
            "--bundle-dir", str(bundle_dir),
            "--live-end-range", f"{range_start_str},{range_end_str}",
            "--lookback", str(lookback_days),
            "--out-dir", str(strategy_out_root),
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
        env.setdefault("PYTHONIOENCODING", "utf-8")

        # P1-5: monitored runner (RSS + 超时双监控) 替代 subprocess.run.
        result = _run_subprocess_monitored(
            cmd,
            env,
            timeout_s=timeout_s,
            memory_limit_mb=memory_limit_mb,
            label=f"{strategy_name} batch=[{range_start_str},{range_end_str}]",
        )

        # 统计本轮写入了多少日子目录（ok / empty）
        n_days_total = 0
        n_days_with_data = 0
        for entry in strategy_out_root.iterdir():
            if not entry.is_dir() or len(entry.name) != 8 or not entry.name.isdigit():
                continue
            diag_path = entry / "diagnostics.json"
            if not diag_path.exists():
                continue
            try:
                diag = json.loads(diag_path.read_text(encoding="utf-8"))
                if not diag.get("batch_mode"):
                    # 单日模式遗留，不计入本次 batch 统计
                    continue
                n_days_total += 1
                if diag.get("status") == "ok":
                    n_days_with_data += 1
            except Exception:
                continue

        return {
            "n_days_total": n_days_total,
            "n_days_with_data": n_days_with_data,
            "returncode": result.returncode,
            "stderr_tail": (result.stderr or "")[-2000:],
        }
