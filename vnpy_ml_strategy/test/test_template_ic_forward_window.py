"""MLStrategyTemplate.ic_forward_window 单测.

验证从 ``bundle_dir/task.json`` 自动解析 forward window:
  - csi300 默认 11 日 label
  - Alpha158 默认 2 日 label
  - ETF 20 日 label
  - 解析失败场景 (bundle 缺失 / label 字段缺 / 表达式不匹配 / list 为空)
  - 缓存命中 (重复读不再触发文件 I/O)

不实例化完整 MLStrategyTemplate (它有 ABC 约束 + on_bar 等抽象方法), 用一个
最小子类替身.

Run:
    F:/Program_Home/vnpy/python.exe -m pytest tests/test_template_ic_forward_window.py -v
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vnpy_ml_strategy.template import MLStrategyTemplate


class _FakeStrategy(MLStrategyTemplate):
    """最小子类: 跳过 ABC 抽象方法 + 不依赖 vnpy 引擎."""

    # 这些是 MLStrategyTemplate 的 abstractmethod, 给空实现满足 ABC
    def on_init(self) -> None:
        pass

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def __init__(self, bundle_dir: str, strategy_name: str = "fake_strat") -> None:
        # 不调 super().__init__ (要 signal_engine, 测试不需要), 直接设需要的属性
        self.bundle_dir = bundle_dir
        self.strategy_name = strategy_name


def _write_task_json(bundle_dir: Path, label_expr: Any) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    task_path = bundle_dir / "task.json"
    task = {
        "dataset": {
            "kwargs": {
                "handler": {
                    "kwargs": {
                        "label": label_expr if isinstance(label_expr, list) else [label_expr],
                    }
                },
                "segments": {
                    "train": ["2018-01-01", "2022-12-31"],
                    "test":  ["2024-01-01", "2024-04-30"],
                },
            }
        }
    }
    task_path.write_text(json.dumps(task), encoding="utf-8")
    return task_path


class TestIcForwardWindowParsing:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("Ref($close, -11) / Ref($close, -1) - 1", 11),  # csi300 默认
            ("Ref($close, -2) / Ref($close, -1) - 1", 2),    # Alpha158 默认
            ("Ref($close, -20) / Ref($close, -1) - 1", 20),  # ETF 默认
            ("Ref($close, -1) / Ref($close, -1) - 1", 1),    # 退化 1 日
            ("Ref($close, -100) / Ref($close, -1) - 1", 100),
            # 多余空格容忍
            ("  Ref($close,  -5)  /  Ref($close,  -1)  -  1  ", 5),
        ],
    )
    def test_parses_typical_forward_returns(self, tmp_path: Path, label: str, expected: int):
        bundle = tmp_path / "bundle_csi300_11"
        _write_task_json(bundle, label)
        strat = _FakeStrategy(bundle_dir=str(bundle))
        assert strat.ic_forward_window == expected

    def test_uses_first_label_in_multi_label_list(self, tmp_path: Path):
        """多 label 时取首个 (与 qlib 框架默认一致)."""
        bundle = tmp_path / "bundle_multi"
        _write_task_json(
            bundle,
            ["Ref($close, -7) / Ref($close, -1) - 1", "$volume / Ref($volume, -1) - 1"],
        )
        strat = _FakeStrategy(bundle_dir=str(bundle))
        assert strat.ic_forward_window == 7

    def test_caches_after_first_read(self, tmp_path: Path):
        """读一次后缓存命中, 改盘也不重读 (label 训练后不会变)."""
        bundle = tmp_path / "bundle_cache"
        _write_task_json(bundle, "Ref($close, -3) / Ref($close, -1) - 1")
        strat = _FakeStrategy(bundle_dir=str(bundle))
        assert strat.ic_forward_window == 3
        # 偷偷改 task.json 模拟篡改
        _write_task_json(bundle, "Ref($close, -99) / Ref($close, -1) - 1")
        # 缓存命中, 仍返 3
        assert strat.ic_forward_window == 3


class TestIcForwardWindowFailures:
    def test_bundle_dir_empty_raises_filenotfound(self, tmp_path: Path):
        strat = _FakeStrategy(bundle_dir="")
        with pytest.raises(FileNotFoundError, match="bundle_dir 未配置"):
            _ = strat.ic_forward_window

    def test_bundle_dir_missing_raises_filenotfound(self, tmp_path: Path):
        nonexistent = tmp_path / "nope" / "bundle"
        strat = _FakeStrategy(bundle_dir=str(nonexistent))
        with pytest.raises(FileNotFoundError, match="task.json 不存在"):
            _ = strat.ic_forward_window

    def test_task_json_missing_label_field_raises(self, tmp_path: Path):
        bundle = tmp_path / "bad1"
        bundle.mkdir(parents=True)
        # 缺 dataset.kwargs.handler.kwargs.label
        (bundle / "task.json").write_text(
            json.dumps({"dataset": {"kwargs": {"segments": {"test": ["2024-01-01", "2024-12-31"]}}}}),
            encoding="utf-8",
        )
        strat = _FakeStrategy(bundle_dir=str(bundle))
        with pytest.raises(ValueError, match="缺.*label"):
            _ = strat.ic_forward_window

    def test_label_empty_list_raises(self, tmp_path: Path):
        bundle = tmp_path / "bad2"
        _write_task_json(bundle, [])
        strat = _FakeStrategy(bundle_dir=str(bundle))
        with pytest.raises(ValueError, match="非列表或空"):
            _ = strat.ic_forward_window

    def test_label_non_string_raises(self, tmp_path: Path):
        bundle = tmp_path / "bad3"
        _write_task_json(bundle, [{"unexpected": "structure"}])
        strat = _FakeStrategy(bundle_dir=str(bundle))
        with pytest.raises(ValueError, match="非字符串"):
            _ = strat.ic_forward_window

    @pytest.mark.parametrize(
        "bad_expr",
        [
            "Mean($close, 5)",                          # lookback (非 forward)
            "$close - Ref($close, -1)",                 # 非除法
            "Ref($close, 11) / Ref($close, -1) - 1",   # 正向 forward (非负数 -N)
            "Ref($close, -11) / Ref($close, 1) - 1",   # 分母用错
            "Ref($volume, -5) / Ref($volume, -1) - 1", # 非 $close
            "Ref($close, -5) / Ref($close, -1)",       # 缺 -1
            "Ref($close, -5) / Ref($close, -1) - 0",   # 末尾不是 -1
            "",                                         # 空字符串
        ],
    )
    def test_unsupported_label_expressions_raise(self, tmp_path: Path, bad_expr: str):
        bundle = tmp_path / "bad_pattern"
        _write_task_json(bundle, bad_expr)
        # 每次重建 strat 避免缓存复用
        strat = _FakeStrategy(bundle_dir=str(bundle), strategy_name=f"s_{hash(bad_expr)}")
        with pytest.raises(ValueError, match="不匹配.*Ref"):
            _ = strat.ic_forward_window


class TestEngineResolverWrapper:
    """验证 engine 端的 _resolve_ic_forward_window 包装层 — 失败时返 None 不抛."""

    def test_returns_int_when_property_works(self, tmp_path: Path):
        from vnpy_ml_strategy.engine import MLEngine
        bundle = tmp_path / "ok"
        _write_task_json(bundle, "Ref($close, -7) / Ref($close, -1) - 1")
        strat = _FakeStrategy(bundle_dir=str(bundle))

        # 不实例化完整 MLEngine, 直接调静态方法的路径
        # 用一个极简对象走 _resolve_ic_forward_window 的 getattr 路径
        class _DummyEngine:
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
        result = _DummyEngine()._resolve_ic_forward_window(strat, "test")
        assert result == 7

    def test_returns_none_when_property_raises(self, tmp_path: Path):
        from vnpy_ml_strategy.engine import MLEngine
        strat = _FakeStrategy(bundle_dir="")  # 会 raise FileNotFoundError

        class _DummyEngine:
            _resolve_ic_forward_window = MLEngine._resolve_ic_forward_window
        result = _DummyEngine()._resolve_ic_forward_window(strat, "test")
        assert result is None  # 包装层吞异常返 None
