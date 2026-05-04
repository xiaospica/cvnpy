"""[P2-1.2] signal_source_strategy 影子策略复用上游 selections 的 link 行为单测.

覆盖:
- 上游 4 个产物 (selections/predictions/diagnostics/metrics) 全部 link 到下游
- hardlink 同 inode (上游覆盖, 下游自动同步)
- 上游产物缺失场景 (子集 link 不报错)
- 上游目录不存在 → last_status='empty' 不 raise
- 重复 link 幂等 (覆盖已存在 dst)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# parents[2] = vnpy_strategy_dev repo root (本文件在 vnpy_ml_strategy/test/)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _make_strategy_stub(strategy_name: str, output_root: str, signal_source: str = ""):
    """构造 MLStrategyTemplate 实例桩, 仅含 _link_selections_from_upstream 需要字段."""
    from vnpy_ml_strategy.template import MLStrategyTemplate

    stub = MLStrategyTemplate.__new__(MLStrategyTemplate)
    stub.strategy_name = strategy_name
    stub.output_root = output_root
    stub.signal_source_strategy = signal_source
    stub.last_status = ""
    stub.write_log = lambda msg: print(f"[stub:{strategy_name}] {msg}")
    return stub


def _create_upstream_artifacts(
    output_root: Path,
    strategy_name: str,
    day: date,
    files: dict,
) -> Path:
    day_str = day.strftime("%Y%m%d")
    d = output_root / strategy_name / day_str
    d.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


def test_link_all_four_artifacts(tmp_path):
    upstream = "csi300_live"
    shadow = "csi300_live_shadow"
    day = date(2026, 4, 30)

    src_dir = _create_upstream_artifacts(
        tmp_path, upstream, day,
        files={
            "selections.parquet": "S",
            "predictions.parquet": "P",
            "diagnostics.json": '{"status":"ok","rows":300}',
            "metrics.json": '{"ic":0.05}',
        },
    )

    stub = _make_strategy_stub(shadow, str(tmp_path), signal_source=upstream)
    stub._link_selections_from_upstream(day)

    dst_dir = tmp_path / shadow / day.strftime("%Y%m%d")
    for fname in ("selections.parquet", "predictions.parquet",
                  "diagnostics.json", "metrics.json"):
        assert (dst_dir / fname).exists(), f"{fname} not linked"
        assert (dst_dir / fname).read_text(encoding="utf-8") == \
               (src_dir / fname).read_text(encoding="utf-8")


def test_hardlink_same_inode(tmp_path):
    """NTFS 同卷 hardlink 应共享 inode (上游覆盖 → 下游自动同步)."""
    import os as _os
    upstream = "csi300_live"
    shadow = "csi300_live_shadow"
    day = date(2026, 4, 30)

    _create_upstream_artifacts(
        tmp_path, upstream, day,
        files={"selections.parquet": "v1"},
    )
    stub = _make_strategy_stub(shadow, str(tmp_path), signal_source=upstream)
    stub._link_selections_from_upstream(day)

    src = tmp_path / upstream / "20260430" / "selections.parquet"
    dst = tmp_path / shadow / "20260430" / "selections.parquet"
    if hasattr(_os, "stat"):
        # Windows os.stat.st_ino 在 NTFS 上对 hardlink 返回相同值.
        # 跨盘场景 fallback 到 copy → ino 不同; 此处 tmp_path 同盘必 hardlink.
        assert _os.stat(src).st_ino == _os.stat(dst).st_ino, \
            "hardlink 应共享 inode (上游覆盖 → 下游自动同步)"


def test_partial_artifacts_no_error(tmp_path):
    """上游只有部分产物 (e.g. 缺 metrics.json), 不应 raise, 缺失文件简单跳过."""
    upstream = "csi300_live"
    shadow = "csi300_live_shadow"
    day = date(2026, 4, 30)

    _create_upstream_artifacts(
        tmp_path, upstream, day,
        files={
            "selections.parquet": "S",
            "diagnostics.json": '{"status":"ok"}',
            # 缺 predictions.parquet 和 metrics.json
        },
    )
    stub = _make_strategy_stub(shadow, str(tmp_path), signal_source=upstream)
    stub._link_selections_from_upstream(day)  # 不应 raise

    dst_dir = tmp_path / shadow / "20260430"
    assert (dst_dir / "selections.parquet").exists()
    assert (dst_dir / "diagnostics.json").exists()
    assert not (dst_dir / "predictions.parquet").exists()
    assert not (dst_dir / "metrics.json").exists()


def test_upstream_missing_marks_empty_no_raise(tmp_path):
    """上游产物未就绪 → last_status='empty' 不 raise, 让 vnpy 主流程不挂."""
    from vnpy_ml_strategy.base import InferenceStatus

    upstream = "csi300_live_not_run_yet"
    shadow = "csi300_live_shadow"
    day = date(2026, 4, 30)

    stub = _make_strategy_stub(shadow, str(tmp_path), signal_source=upstream)
    stub._link_selections_from_upstream(day)  # 不 raise

    assert stub.last_status == InferenceStatus.EMPTY.value
    # dst 目录可能创建可能不创建, 但绝不应该有产物
    dst_dir = tmp_path / shadow / "20260430"
    if dst_dir.exists():
        assert not list(dst_dir.glob("*.parquet"))


def test_repeated_link_idempotent(tmp_path):
    """重复 link 同 day → 覆盖已存在 dst, 不报错."""
    upstream = "csi300_live"
    shadow = "csi300_live_shadow"
    day = date(2026, 4, 30)

    _create_upstream_artifacts(
        tmp_path, upstream, day,
        files={"selections.parquet": "v1"},
    )
    stub = _make_strategy_stub(shadow, str(tmp_path), signal_source=upstream)
    stub._link_selections_from_upstream(day)
    # 上游改内容 (rewrite)
    src = tmp_path / upstream / "20260430" / "selections.parquet"
    src.write_text("v2", encoding="utf-8")
    # 第二次 link 不应 raise
    stub._link_selections_from_upstream(day)

    dst = tmp_path / shadow / "20260430" / "selections.parquet"
    assert dst.read_text(encoding="utf-8") == "v2"
