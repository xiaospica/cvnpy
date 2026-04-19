"""publish_metrics — 主进程侧监控指标对外发布.

三件事:
1. 复制 ``{out_dir}/metrics.json`` → ``{out_dir}/latest.json`` (原子 rename)
2. 填入 ``MetricsCache`` 供 webtrader REST 查询
3. ``event_engine.put(EVENT_ML_METRICS + strategy_name, payload)`` 通知订阅者

latest.json 只在子进程 status=ok 或 empty 时写 (failed 不覆盖旧 latest,
避免"失败日覆盖成功日的监控数据").
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Dict

from .cache import MetricsCache


def publish_metrics(
    cache: MetricsCache,
    strategy_name: str,
    trade_date: date,
    output_root: str,
    metrics: Dict[str, Any],
    status: str = "ok",
) -> Path:
    """更新 cache + 原子写 latest.json.

    Parameters
    ----------
    cache : MetricsCache
        Engine-level metrics cache.
    strategy_name : str
        Used for both cache key and directory layout.
    trade_date : date
        Today, used to locate ``{output_root}/{strategy_name}/{yyyymmdd}/``.
    output_root : str
        Base directory containing per-day subdirs.
    metrics : dict
        Payload from subprocess metrics.json (or empty dict on failure).
    status : str
        "ok" / "empty" / "failed". Only "ok"/"empty" updates latest.json.

    Returns
    -------
    Path to latest.json (whether written this call or preserved from earlier).
    """
    cache.update(strategy_name, metrics)

    day_dir = Path(output_root) / strategy_name / trade_date.strftime("%Y%m%d")
    latest = Path(output_root) / strategy_name / "latest.json"

    if status in ("ok", "empty"):
        latest.parent.mkdir(parents=True, exist_ok=True)
        # Prefer copying the subprocess-written metrics.json if present;
        # otherwise dump the in-memory metrics dict.
        source = day_dir / "metrics.json"
        tmp = latest.with_suffix(latest.suffix + ".tmp")

        if source.exists():
            shutil.copy2(source, tmp)
        else:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(metrics, f, ensure_ascii=False, indent=2, default=str)

        os.replace(tmp, latest)

    return latest
