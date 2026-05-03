"""A 股交易日历封装.

优先级:
1. 优先用本地 qlib bin 的 calendar 文件 (``qlib_data_bin/calendars/day.txt``) —
   只读磁盘文件, 无需 import qlib, 不会把 qlib 拖进 vnpy 主进程
2. 如果 calendar 文件不存在, fallback 到 weekday < 5 (周一至周五)

vnpy 主进程每天盘前 09:15 先调 ``is_trade_day`` 做一次短路, 非交易日不启
subprocess 节省开机成本.

Stale 检查
----------
``QlibCalendar.is_trade_day(d)`` 当 ``d > max(trade_days)`` 时 **raise**
``StaleCalendarError``, 而不是静默返回 False. 设计原因:

- 生产场景: 21:00 cron 推理时 calendar 必然已被 20:00 ingest 推到当日.
  这个 raise 永远不应该触发. 一旦触发说明运维流程坏了
  (DailyIngestPipeline 没跑完或没跑), 应立即 alert 而不是默默 skip.
- 静默返回 False 是最坏的失败模式 — 监控会把 "今天没出推理" 错认为是节假日.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional, Set


class StaleCalendarError(RuntimeError):
    """Calendar 末尾日期 < 查询日期, 说明 calendar 滞后于查询.

    生产场景该错误永远不应该触发 (cron 顺序保证 ingest 先于推理).
    一旦触发: 检查 DailyIngestPipeline 是否运行成功, 必要时手动 ingest 当日数据
    后调 ``QlibCalendar.refresh()`` 让 cache 失效再重试.
    """


class QlibCalendar:
    """从 qlib_data_bin/calendars/day.txt 读交易日.

    该文件每行一个 ``YYYY-MM-DD`` 字符串, 仅含交易日.
    """

    def __init__(self, provider_uri: str):
        self._provider_uri = provider_uri
        self._trade_days: Optional[Set[str]] = None
        self._max_known: Optional[str] = None  # 缓存末尾日期 (YYYY-MM-DD), set 一起更新

    def _load(self) -> Set[str]:
        if self._trade_days is not None:
            return self._trade_days
        cal_path = Path(self._provider_uri) / "calendars" / "day.txt"
        if not cal_path.exists():
            self._trade_days = set()
            self._max_known = None
            return self._trade_days
        lines = cal_path.read_text(encoding="utf-8").splitlines()
        self._trade_days = {line.strip() for line in lines if line.strip()}
        self._max_known = max(self._trade_days) if self._trade_days else None
        return self._trade_days

    def is_trade_day(self, d: date) -> bool:
        """判断 ``d`` 是否交易日.

        判定顺序:
          1. 周六/周日 (``weekday >= 5``) → 必然非交易日, 直接 return False
             (周末不需要 calendar 也能确定, 不触发 stale 检查)
          2. 工作日且在 calendar 已知范围内 → 查 calendar set
          3. 工作日且超过 calendar 末尾 → raise StaleCalendarError
             (无法区分是法定节假日还是 calendar 滞后, 应让上游 ingest 后 refresh 重试)

        Raises
        ------
        StaleCalendarError
            当 ``d`` 是工作日 (Mon-Fri) 且 ``d > max(trade_days)`` 时.
            周末日不会触发 — 静默返 False.
        """
        trade_days = self._load()
        if not trade_days:
            # fallback: weekday-based check (calendar 文件不存在时无法做 stale 检查)
            return d.weekday() < 5

        # 周末必然非交易日, calendar 也不会含周末日. 不查 calendar, 不触发 stale.
        if d.weekday() >= 5:
            return False

        d_str = d.strftime("%Y-%m-%d")
        # 工作日超过 calendar 末尾: 可能是 1) calendar 滞后, 2) 法定节假日 calendar
        # 应记录但还没记录 — 两种情况都需要先 ingest 让 calendar 补全才能可靠判定.
        if self._max_known is not None and d_str > self._max_known:
            raise StaleCalendarError(
                f"calendar 末尾 {self._max_known} < 查询日期 {d_str} (weekday={d.weekday()}); "
                f"先跑 DailyIngestPipeline 让 calendar 补到当日 (或调用 .refresh())"
            )
        return d_str in trade_days

    def refresh(self) -> None:
        """Force reload on next query (e.g., after nightly calendar update)."""
        self._trade_days = None
        self._max_known = None

    def prev_trade_day(self, d: date, max_lookback: int = 14) -> Optional[date]:
        """返回 ``d`` (不含) 之前最近的交易日; 找不到返 None.

        实盘 09:26 cron 用: 读"昨晚 21:00 cron persist 的 pred" → 该 pred 落在
        prev_trade_day(today). 历经周末 / 节假日时跨多天, 默认回看 14 个自然日
        足够覆盖最长法定假期.
        """
        from datetime import timedelta as _td
        trade_days = self._load()
        if not trade_days:
            # fallback 用 weekday: 周一找上周五, 其他找前一天
            cur = d - _td(days=1)
            for _ in range(max_lookback):
                if cur.weekday() < 5:
                    return cur
                cur -= _td(days=1)
            return None
        cur = d - _td(days=1)
        for _ in range(max_lookback):
            if cur.strftime("%Y-%m-%d") in trade_days:
                return cur
            cur -= _td(days=1)
        return None


class WeekdayFallbackCalendar:
    """当 provider_uri 不可用时的保底实现."""

    def is_trade_day(self, d: date) -> bool:
        return d.weekday() < 5

    def prev_trade_day(self, d: date, max_lookback: int = 14) -> Optional[date]:
        from datetime import timedelta as _td
        cur = d - _td(days=1)
        for _ in range(max_lookback):
            if cur.weekday() < 5:
                return cur
            cur -= _td(days=1)
        return None


def make_calendar(provider_uri: Optional[str] = None):
    """Factory — 若 provider_uri 有效则用 QlibCalendar, 否则 weekday fallback."""
    if provider_uri and (Path(provider_uri) / "calendars" / "day.txt").exists():
        return QlibCalendar(provider_uri)
    return WeekdayFallbackCalendar()
