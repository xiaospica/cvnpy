"""[P1-2] loguru 日志滚动配置.

vnpy 主进程 / 推理子进程 / scheduler 都用 loguru, 但默认无 rotation —
几周后磁盘塞满. 本模块在主入口启动时调一次, 给 loguru 加文件 sink + 滚动.

NSSM 自身的 AppRotateFiles=1 / AppRotateBytes=10MB 也开了, 但那只滚动 vnpy
进程的 stdout/stderr 文件 (D:/vnpy_logs/vnpy_headless.log/.err); 框架内部
loguru log (gateway / event_engine / scheduler) 默认还是写到 stderr 走 NSSM
捕获, 没单独文件. 加额外文件 sink 让 loguru 自己管理便于按日期/大小切割.

为什么加 retention="14 days":
  - vnpy / mlearnweb 两端都没接 ELK / SaaS 集中日志, 历史靠本地文件查
  - 14 天能覆盖 2 周内的故障复盘需要 (再老的查 ml_output/ diagnostics.json)
  - 单文件 100 MB × 14 天 ≈ 1.4 GB 上限, 200 GB 系统盘绰绰有余
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


_DEFAULT_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)


def setup_logger(
    log_root: Optional[str] = None,
    *,
    process_name: str = "vnpy_headless",
    rotation: str = "100 MB",
    retention: str = "14 days",
    compression: str = "zip",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
) -> Path:
    """配置 loguru: 清掉默认 sink, 重加 console + 滚动文件.

    Args:
        log_root: 日志根目录. 默认取 env LOG_ROOT, 缺省 D:/vnpy_logs.
        process_name: 文件名前缀, 用以区分 vnpy_headless / mlearnweb 等进程.
        rotation: 单文件大小阈值 (loguru rotation 参数).
        retention: 文件保留期 (老文件 zip 压缩后超期删除).
        compression: zip / gz / bz2.
        console_level: 控制台 sink 最低级别.
        file_level: 文件 sink 最低级别 (建议 DEBUG, 故障时拉日志能看到详情).

    Returns:
        实际写入的文件路径 (含 {time} 占位由 loguru 在写入时展开).
    """
    from loguru import logger

    # P1-2: 清掉默认 stderr sink, 自己挂.
    logger.remove()

    # 控制台 sink (NSSM 会捕获到 AppStdout/AppStderr 文件)
    logger.add(
        sys.stderr,
        level=console_level,
        format=_DEFAULT_LOG_FORMAT,
        colorize=True,
        enqueue=True,   # 多线程安全 (vnpy EventEngine 主线程 + scheduler 线程都会写)
    )

    # 文件 sink — 按日期切, 单文件 100 MB 滚动, 14 天保留, zip 压缩.
    root = Path(log_root or os.getenv("LOG_ROOT") or r"D:\vnpy_logs")
    root.mkdir(parents=True, exist_ok=True)
    file_pattern = root / f"{process_name}_{{time:YYYY-MM-DD}}.log"
    logger.add(
        str(file_pattern),
        level=file_level,
        format=_DEFAULT_LOG_FORMAT,
        rotation=rotation,
        retention=retention,
        compression=compression,
        encoding="utf-8",
        enqueue=True,
    )

    logger.info(
        "[log_setup] loguru sink 已配置: console={console} file={file_pattern} "
        "rotation={rotation} retention={retention}",
        console=console_level,
        file_pattern=str(file_pattern),
        rotation=rotation,
        retention=retention,
    )
    return root / f"{process_name}.log"
