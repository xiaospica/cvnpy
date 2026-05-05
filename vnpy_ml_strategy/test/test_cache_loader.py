"""cache_loader.py 单元测试.

验证 ``reload_history_from_disk`` 能在 IcBackfillService 子进程改完磁盘
metrics.json 后, 把最新数据重新加载回 MetricsCache.

Run:
    F:/Program_Home/vnpy/python.exe -m pytest tests/test_cache_loader.py -v
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from vnpy_ml_strategy.monitoring.cache import MetricsCache
from vnpy_ml_strategy.monitoring.cache_loader import reload_history_from_disk


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    return tmp_path


def _write_metrics(output_root: Path, strategy: str, day: date, payload: dict) -> Path:
    """工具: 在 output_root/{strategy}/{YYYYMMDD}/metrics.json 写入指标."""
    day_dir = output_root / strategy / day.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / "metrics.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


class TestReloadHistoryFromDisk:
    def test_loads_recent_metrics_into_cache(self, output_root: Path):
        """常见路径: 3 天 metrics.json, reload 后 cache 含全部 3 天."""
        cache = MetricsCache(max_history_days=30)
        today = date.today()
        for i in range(3):
            d = today - timedelta(days=i)
            _write_metrics(output_root, "csi300_a", d, {"ic": 0.05 + i * 0.01, "n_predictions": 300 + i})

        n = reload_history_from_disk(
            cache, strategy_name="csi300_a", output_root=str(output_root), max_days=30,
        )
        assert n == 3
        history = cache.get_history("csi300_a", days=30)
        assert len(history) == 3
        # ring buffer 末尾是最新一天 (today, ic=0.05)
        assert history[-1]["ic"] == pytest.approx(0.05)

    def test_skips_dirs_outside_max_days_window(self, output_root: Path):
        """too-old 目录不被扫到: 31 天前的 metrics.json 应被跳过."""
        cache = MetricsCache(max_history_days=30)
        today = date.today()
        _write_metrics(output_root, "etf_v1", today, {"ic": 0.1})
        _write_metrics(output_root, "etf_v1", today - timedelta(days=31), {"ic": -0.5})

        n = reload_history_from_disk(
            cache, strategy_name="etf_v1", output_root=str(output_root), max_days=30,
        )
        assert n == 1
        history = cache.get_history("etf_v1")
        assert len(history) == 1
        assert history[0]["ic"] == pytest.approx(0.1)

    def test_strategy_dir_missing_returns_zero(self, output_root: Path):
        """策略子目录不存在 → 返 0 不抛异常."""
        cache = MetricsCache()
        n = reload_history_from_disk(
            cache, strategy_name="nonexistent", output_root=str(output_root),
        )
        assert n == 0
        assert cache.get_history("nonexistent") == []

    def test_skips_non_yyyymmdd_dirs(self, output_root: Path):
        """``latest.json`` / 命名不规范的目录 (非 8 位数字) 不会破坏扫描."""
        cache = MetricsCache()
        strat_dir = output_root / "weird_strat"
        strat_dir.mkdir(parents=True, exist_ok=True)
        # 创建一些干扰文件 / 子目录
        (strat_dir / "latest.json").write_text("{}", encoding="utf-8")
        (strat_dir / "2024-01-15").mkdir()  # 含分隔符, 不匹配 YYYYMMDD
        (strat_dir / "abcdefgh").mkdir()    # 8 字符但非数字
        (strat_dir / "20260101").mkdir()    # 合法 YYYYMMDD
        (strat_dir / "20260101" / "metrics.json").write_text(
            json.dumps({"ic": 0.07}), encoding="utf-8",
        )

        # max_days 设大些保证 20260101 在窗口内
        n = reload_history_from_disk(
            cache, strategy_name="weird_strat", output_root=str(output_root), max_days=10000,
        )
        assert n == 1
        assert cache.get_latest("weird_strat")["ic"] == pytest.approx(0.07)

    def test_corrupted_json_skipped_no_crash(self, output_root: Path):
        """损坏的 metrics.json 不影响其他天的加载."""
        cache = MetricsCache()
        today = date.today()
        # 一个合法 + 一个损坏
        _write_metrics(output_root, "s1", today, {"ic": 0.08})
        bad = output_root / "s1" / (today - timedelta(days=1)).strftime("%Y%m%d") / "metrics.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not valid json", encoding="utf-8")

        n = reload_history_from_disk(
            cache, strategy_name="s1", output_root=str(output_root), max_days=30,
        )
        assert n == 1  # 只成功加载合法的那个
        history = cache.get_history("s1")
        assert len(history) == 1
        assert history[0]["ic"] == pytest.approx(0.08)

    def test_metrics_json_not_a_dict_skipped(self, output_root: Path):
        """metrics.json 顶层不是 dict (比如是 list) → 跳过, 不污染 cache."""
        cache = MetricsCache()
        today = date.today()
        bad_dir = output_root / "s2" / today.strftime("%Y%m%d")
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "metrics.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        n = reload_history_from_disk(
            cache, strategy_name="s2", output_root=str(output_root),
        )
        assert n == 0
        assert cache.get_latest("s2") is None

    def test_loads_in_chronological_order(self, output_root: Path):
        """关键: 必须按日期升序灌入 cache, 否则 ring buffer 末尾不是最新.

        IcBackfillService 改写历史 metrics.json 后, mlearnweb / webtrader
        get_metrics_history 的"最近 N 日"语义依赖 cache.deque 末尾是最新.
        """
        cache = MetricsCache()
        today = date.today()
        # 故意乱序写盘 (虽然 OS 会自己排序, 但显式测试)
        _write_metrics(output_root, "s3", today - timedelta(days=2), {"ic": 0.01, "day_label": "old"})
        _write_metrics(output_root, "s3", today, {"ic": 0.03, "day_label": "newest"})
        _write_metrics(output_root, "s3", today - timedelta(days=1), {"ic": 0.02, "day_label": "mid"})

        reload_history_from_disk(
            cache, strategy_name="s3", output_root=str(output_root), max_days=30,
        )
        history = cache.get_history("s3", days=30)
        labels = [h["day_label"] for h in history]
        assert labels == ["old", "mid", "newest"], (
            f"必须按日期升序灌入, 实际: {labels}"
        )
        # latest 自然是最后一次 update (即升序的末尾 = 最新一天)
        assert cache.get_latest("s3")["day_label"] == "newest"

    def test_metrics_json_missing_for_some_days(self, output_root: Path):
        """有些日期目录存在但缺 metrics.json (推理失败), reload 不被中断."""
        cache = MetricsCache()
        today = date.today()
        _write_metrics(output_root, "s4", today, {"ic": 0.04})
        # 创建一个空目录 (缺 metrics.json)
        empty_dir = output_root / "s4" / (today - timedelta(days=1)).strftime("%Y%m%d")
        empty_dir.mkdir(parents=True)

        n = reload_history_from_disk(
            cache, strategy_name="s4", output_root=str(output_root),
        )
        assert n == 1
        assert cache.get_latest("s4")["ic"] == pytest.approx(0.04)
