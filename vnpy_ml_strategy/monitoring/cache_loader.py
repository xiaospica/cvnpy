"""扫 ``{output_root}/{strategy}/{yyyymmdd}/metrics.json`` 把历史指标加载回 MetricsCache.

为什么需要：
    ``IcBackfillService`` 子进程跑完 ``run_ic_backfill.py`` 后，会**改写磁盘上历史
    metrics.json 文件**（把 IC / RankIC 字段填回去）。但主进程的 ``MetricsCache``
    是通过 ``publish_metrics`` 在 inference 完成的当下灌入的，**不会主动感知磁盘
    变化**。结果是 webtrader REST 端点读 cache 返回的还是 IC=null 的旧数据，
    mlearnweb 监控端拉到的也是旧数据，IC backfill 形同虚设。

修法：
    IcBackfillService 提供 ``on_complete`` 回调，子进程跑完后由调用方（``MLEngine``）
    调本模块的 ``reload_history_from_disk``，把磁盘上最近 N 日的 metrics.json
    重新加载回 cache。这样 webtrader 下次查询就能拿到 IC 已填回的版本。

设计取舍：
    * 全量 reload 最近 N 天，不做增量 — 简单、幂等、与 ``IcBackfillService.scan_days``
      默认 30 一致；30 个 JSON 文件磁盘读 + dict 转换 < 50ms，可接受。
    * 失败 (文件缺失 / JSON 损坏) 仅 log warn 并 skip，不影响其他天。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List

from .cache import MetricsCache

logger = logging.getLogger(__name__)


def reload_history_from_disk(
    cache: MetricsCache,
    *,
    strategy_name: str,
    output_root: str,
    max_days: int = 500,
) -> int:
    """重新加载磁盘上最近 ``max_days`` 个交易日的 metrics.json 到 cache。

    扫 ``{output_root}/{strategy_name}/`` 下命名为 ``YYYYMMDD`` 的子目录，按日期
    升序读 ``metrics.json``，调 ``cache.update(strategy_name, metrics)``。

    Parameters
    ----------
    cache : MetricsCache
        要刷新的 cache 实例。
    strategy_name : str
        策略名 (cache 分桶 key + 目录名)。
    output_root : str
        策略输出根目录。
    max_days : int
        最多 reload 多少天 (从今天往前数自然日)。默认 500 (~2 个交易年), 与
        ``MLEngine._metrics_cache`` 的 ``max_history_days`` 对齐。早期默认
        30 是对齐 IcBackfillService.scan_days, 但启动期 seed 应灌全部历史。

    Returns
    -------
    int
        成功 reload 的 metrics.json 文件数。
    """
    base = Path(output_root) / strategy_name
    if not base.exists():
        logger.debug(
            "[cache_loader][%s] base dir not found: %s", strategy_name, base,
        )
        return 0

    # 按 YYYYMMDD 子目录升序枚举（升序保证 cache 的 ring buffer 末尾是最新一日）
    today = date.today()
    earliest = today - timedelta(days=max_days)
    candidate_dirs = _list_yyyymmdd_dirs(base, earliest=earliest)
    if not candidate_dirs:
        return 0

    loaded = 0
    for day_dir in candidate_dirs:
        metrics_path = day_dir / "metrics.json"
        if not metrics_path.exists():
            continue
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                metrics = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[cache_loader][%s] read %s failed: %s",
                strategy_name, metrics_path, exc,
            )
            continue
        if not isinstance(metrics, dict):
            logger.warning(
                "[cache_loader][%s] %s root is not a dict (%s), skip",
                strategy_name, metrics_path, type(metrics).__name__,
            )
            continue
        cache.update(strategy_name, metrics)
        loaded += 1

    logger.debug(
        "[cache_loader][%s] reloaded %d/%d metrics files from %s",
        strategy_name, loaded, len(candidate_dirs), base,
    )
    return loaded


def _list_yyyymmdd_dirs(base: Path, *, earliest: date) -> List[Path]:
    """返回 ``base`` 下命名为 YYYYMMDD 的子目录, 按日期升序, 仅含 >= earliest 的。

    无效命名 (含 ``.`` / 长度不对 / 非数字) 一律 skip 不报错 —— 同目录下可能有
    ``latest.json`` / ``baseline.parquet`` 之类伴生文件。
    """
    out: List[Path] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if len(name) != 8 or not name.isdigit():
            continue
        try:
            d = datetime.strptime(name, "%Y%m%d").date()
        except ValueError:
            continue
        if d < earliest:
            continue
        out.append(child)
    out.sort(key=lambda p: p.name)
    return out
