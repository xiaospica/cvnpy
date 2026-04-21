"""IC 回填 service (主进程 wrapper, 方案 §2.4.5).

主进程不允许 import qlib (方案 §2.2), 所以本 service 通过 subprocess 调
``qlib_strategy_core.cli.run_ic_backfill`` 完成实际计算 + 重写 metrics.json.

主流程:
    IcBackfillService(strategy_name=..., output_root=..., provider_uri=...,
                      inference_python=..., forward_window=2, scan_days=30,
                      timeout_s=120)
    .run_async()  # 非阻塞 — 起后台线程, 不卡 EventEngine

debounce 默认 60s (避免一次推理触发多次回填; 同 strategy 60s 内重复调用 no-op).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class IcBackfillResult:
    """子进程执行汇总. ``success`` 表示子进程 exit=0; computed/skipped 字段照搬 stdout JSON."""

    success: bool
    scanned: int = 0
    computed: int = 0
    skipped_no_forward: int = 0
    skipped_no_pred: int = 0
    skipped_already_filled: int = 0
    errors: int = 0
    duration_ms: int = 0
    raw: Optional[Dict[str, Any]] = None
    error_message: str = ""


class IcBackfillService:
    """异步触发 IC 回填. 一个 service 实例对应一只策略.

    多次 ``run_async()`` 在 debounce 窗口内只跑一次 (last-write-wins).
    """

    def __init__(
        self,
        *,
        strategy_name: str,
        output_root: str,
        provider_uri: str,
        inference_python: str,
        forward_window: int = 2,
        scan_days: int = 30,
        timeout_s: int = 120,
        debounce_s: float = 60.0,
        on_complete: Optional[Callable[[IcBackfillResult], None]] = None,
        install_legacy_path: bool = True,
    ) -> None:
        """
        Parameters
        ----------
        strategy_name : str
            策略名 (扫 ``{output_root}/{strategy_name}/`` 子目录).
        output_root : str
            根目录, 与策略 output_root 一致.
        provider_uri : str
            qlib bin 路径, 与策略 provider_uri 一致.
        inference_python : str
            qlib 兼容的 Python 解释器路径 (与 inference subprocess 同一个).
        forward_window : int
            label 的 forward 交易日窗口. 默认 2 (Alpha158 默认).
        scan_days : int
            回看自然日数.
        timeout_s : int
            子进程总超时.
        debounce_s : float
            两次 ``run_async`` 之间的最小间隔, 避免频繁触发.
        on_complete : callable
            子进程完成回调 (在后台线程调). 可用于推 EventEngine 事件.
        install_legacy_path : bool
            子进程是否启用 legacy module path 兼容 (旧 MLflow artifact).
        """
        self.strategy_name = strategy_name
        self.output_root = output_root
        self.provider_uri = provider_uri
        self.inference_python = inference_python
        self.forward_window = forward_window
        self.scan_days = scan_days
        self.timeout_s = timeout_s
        self.debounce_s = debounce_s
        self.on_complete = on_complete
        self.install_legacy_path = install_legacy_path

        self._last_run_at: float = 0.0
        self._lock = threading.Lock()
        self._running: bool = False

    def run_async(self) -> bool:
        """触发一次后台回填. 返回 True = 已触发, False = debounce 跳过 / 已在跑."""
        now = time.time()
        with self._lock:
            if self._running:
                logger.debug(
                    f"[ic_backfill][{self.strategy_name}] already running, skip"
                )
                return False
            if now - self._last_run_at < self.debounce_s:
                logger.debug(
                    f"[ic_backfill][{self.strategy_name}] debounced "
                    f"({now - self._last_run_at:.0f}s < {self.debounce_s}s)"
                )
                return False
            self._running = True
            self._last_run_at = now

        th = threading.Thread(
            target=self._worker,
            name=f"ic-backfill-{self.strategy_name}",
            daemon=True,
        )
        th.start()
        return True

    def run_sync(self) -> IcBackfillResult:
        """同步执行 (主要给测试 / 手动触发用). 不走 debounce."""
        return self._invoke_subprocess()

    def _worker(self) -> None:
        try:
            result = self._invoke_subprocess()
            if self.on_complete is not None:
                try:
                    self.on_complete(result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[ic_backfill][{self.strategy_name}] on_complete failed: {exc}"
                    )
        finally:
            with self._lock:
                self._running = False

    def _invoke_subprocess(self) -> IcBackfillResult:
        cmd = [
            self.inference_python, "-u", "-m",
            "qlib_strategy_core.cli.run_ic_backfill",
            "--output-root", self.output_root,
            "--strategy", self.strategy_name,
            "--provider-uri", self.provider_uri,
            "--scan-days", str(self.scan_days),
            "--forward-window", str(self.forward_window),
        ]
        if self.install_legacy_path:
            cmd.append("--install-legacy-path")

        t0 = time.time()
        logger.info(
            f"[ic_backfill][{self.strategy_name}] subprocess start "
            f"(scan_days={self.scan_days}, forward_window={self.forward_window})"
        )
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                f"[ic_backfill][{self.strategy_name}] timeout after {self.timeout_s}s"
            )
            return IcBackfillResult(
                success=False,
                duration_ms=int((time.time() - t0) * 1000),
                error_message=f"TimeoutExpired: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ic_backfill][{self.strategy_name}] subprocess launch failed: {exc}"
            )
            return IcBackfillResult(
                success=False,
                duration_ms=int((time.time() - t0) * 1000),
                error_message=f"{type(exc).__name__}: {exc}",
            )

        # 子进程把 summary 一行 JSON 打到 stdout (成功时), stderr 打 fatal (失败时)
        raw: Optional[Dict[str, Any]] = None
        target = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
        for line in (target or "").strip().splitlines()[::-1]:
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    raw = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        success = proc.returncode == 0
        if not success:
            logger.warning(
                f"[ic_backfill][{self.strategy_name}] subprocess exit={proc.returncode}, "
                f"stderr_tail={(proc.stderr or '')[-500:]}"
            )

        result = IcBackfillResult(
            success=success,
            scanned=int(raw.get("scanned", 0)) if raw else 0,
            computed=int(raw.get("computed", 0)) if raw else 0,
            skipped_no_forward=int(raw.get("skipped_no_forward", 0)) if raw else 0,
            skipped_no_pred=int(raw.get("skipped_no_pred", 0)) if raw else 0,
            skipped_already_filled=int(raw.get("skipped_already_filled", 0)) if raw else 0,
            errors=int(raw.get("errors", 0)) if raw else 0,
            duration_ms=int(raw.get("duration_ms", (time.time() - t0) * 1000)) if raw else int((time.time() - t0) * 1000),
            raw=raw,
            error_message="" if success else (raw.get("fatal", "") if raw else f"exit={proc.returncode}"),
        )
        logger.info(
            f"[ic_backfill][{self.strategy_name}] done "
            f"scanned={result.scanned} computed={result.computed} "
            f"skipped_no_forward={result.skipped_no_forward} "
            f"errors={result.errors} duration={result.duration_ms}ms"
        )
        return result
