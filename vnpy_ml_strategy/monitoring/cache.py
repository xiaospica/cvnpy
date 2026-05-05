"""MetricsCache — 主进程侧最新值 + 最近 N 日 ring buffer.

在子进程 (subprocess) 写完 metrics.json 后, 主进程 MLEngine.publish_metrics
把 metrics dict 填入本 cache. webtrader REST 直接从 cache 读.

不做跨天聚合 — 跨天聚合在 mlearnweb 侧 (ml_aggregation_service) 做,
本 cache 只保证"最近 N 日能原地查".
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Any, Dict, List, Optional


class MetricsCache:
    """Thread-safe 单日指标缓存. 按 strategy_name 分桶."""

    def __init__(self, max_history_days: int = 30):
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, deque] = {}
        self._lock = Lock()
        self._max_history = max_history_days

    def update(self, strategy_name: str, metrics: Dict[str, Any]) -> None:
        with self._lock:
            self._latest[strategy_name] = dict(metrics)
            if strategy_name not in self._history:
                self._history[strategy_name] = deque(maxlen=self._max_history)
            # Upsert by trade_date — 同一个 trade_date 应只有一条最新值. 否则
            # init_strategy 启动期 reload_history_from_disk + 当日 replay
            # publish_metrics 会让同日有 2 条; mlearnweb 端 _diff_and_apply
            # 用 (node_id, engine, strategy, trade_date) UNIQUE 约束 INSERT
            # 时第二条会 IntegrityError.
            buf = self._history[strategy_name]
            new_td = metrics.get("trade_date")
            if new_td is not None:
                # 倒序删旧值 — deque.remove 删第一个匹配, 我们要确保所有同日重复都清掉
                # (deque 长度 ≤ 500, O(N) 扫描可忽略).
                stale = [m for m in buf if m.get("trade_date") == new_td]
                for s in stale:
                    try:
                        buf.remove(s)
                    except ValueError:
                        pass
            buf.append(dict(metrics))

    def get_latest(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            latest = self._latest.get(strategy_name)
            return dict(latest) if latest else None

    def get_history(self, strategy_name: str, days: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            buf = self._history.get(strategy_name)
            if not buf:
                return []
            return [dict(m) for m in list(buf)[-days:]]

    def list_strategies(self) -> List[str]:
        with self._lock:
            return list(self._latest.keys())
