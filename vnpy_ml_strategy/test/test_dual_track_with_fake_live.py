"""[P2-1.3 V2] 实盘+模拟双轨架构 + 信号同步 — 用 FakeQmtGateway 替身 (无真实盘环境).

V2 验证 V1 之上补的部分:
  R5 命名 validator 双轨 (live + sim) 各自分支正确
  R6 启动期 _validate_startup_config 接受混合 GATEWAYS (kind=fake_live/sim)
  R7 _validate_signal_source_consistency 强制影子与上游 bundle/topk/n_drop 一致
  R8 信号同步 (signal_source_strategy): 影子复用上游 selections.parquet,
     两策略每日 selections 字节级相等

用 FakeQmtGateway (default_name='QMT', 命名 validator 走 live 分支, 内核仍为
QmtSimGateway 撮合) 替代真 miniqmt — 不需要交易时段 / 不需要券商账户. 配合
QMT_SIM_* sim gateway 验证整套双轨架构.

V3 (真券商仿真账户) 留 TODO 待下一交易日盘中, 详见
docs/deployment_a1_p21_plan.md §三.2 V3 章节.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# 让 vnpy_ml_strategy.test.fakes / run_ml_headless 在 pytest 上下文可解析
# (parents[2] = vnpy_strategy_dev repo root)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ============================================================================
# R5: 命名 validator 双轨各自分支
# ============================================================================

def test_naming_validator_dual_track() -> None:
    """live 和 sim gateway 在命名 validator 中各自走自己的分支."""
    from vnpy_common.naming import classify_gateway, validate_gateway_name

    # live (QMT 或 FakeQmt 都用同名)
    assert classify_gateway("QMT") == "live"
    validate_gateway_name("QMT", expected_class="live")

    # sim
    assert classify_gateway("QMT_SIM_csi300_shadow") == "sim"
    validate_gateway_name("QMT_SIM_csi300_shadow", expected_class="sim")

    # 反例: live 期望 sim 应 raise
    with pytest.raises(ValueError):
        validate_gateway_name("QMT", expected_class="sim")
    with pytest.raises(ValueError):
        validate_gateway_name("QMT_SIM_x", expected_class="live")


# ============================================================================
# R6: 启动期校验接受混合 GATEWAYS (kind 各自分支)
# ============================================================================

def test_validate_startup_config_dual_track_passes(monkeypatch) -> None:
    """混合 [kind=fake_live, kind=sim] + 实盘策略 + 影子策略 应通过校验."""
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "fake_live", "name": "QMT", "setting": {}},
        {"kind": "sim",       "name": "QMT_SIM_shadow", "setting": dict(r.QMT_SIM_BASE_SETTING)},
    ]
    r.STRATEGIES = [
        {
            "strategy_name": "live", "strategy_class": "X", "gateway_name": "QMT",
            "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1, "trigger_time": "21:00"},
        },
        {
            "strategy_name": "shadow", "strategy_class": "X", "gateway_name": "QMT_SIM_shadow",
            "setting_override": {
                "bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1,
                "trigger_time": "21:00",  # 与 live 同也通过 (影子不跑推理)
                "signal_source_strategy": "live",
            },
        },
    ]
    r._validate_startup_config()  # 不应 raise


def test_validate_rejects_two_live_gateways(monkeypatch) -> None:
    """miniqmt 单进程单账户约束 → 双 live (含 fake_live) 应 raise."""
    import importlib
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "live", "name": "QMT", "setting": {}},
        {"kind": "live", "name": "QMT", "setting": {}},
    ]
    r.STRATEGIES = []
    with pytest.raises(ValueError, match="kind=live"):
        r._validate_startup_config()


# ============================================================================
# R7: signal_source_strategy 一致性硬校验
# ============================================================================

def test_signal_source_consistency_passes_when_aligned(monkeypatch) -> None:
    """影子与上游 bundle/topk/n_drop 一致 → 通过."""
    import importlib
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "sim", "name": "QMT_SIM_a", "setting": dict(r.QMT_SIM_BASE_SETTING)},
        {"kind": "sim", "name": "QMT_SIM_b", "setting": dict(r.QMT_SIM_BASE_SETTING)},
    ]
    r.STRATEGIES = [
        {"strategy_name": "live", "strategy_class": "X", "gateway_name": "QMT_SIM_a",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1, "trigger_time": "21:00"}},
        {"strategy_name": "shadow", "strategy_class": "X", "gateway_name": "QMT_SIM_b",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1,
                              "signal_source_strategy": "live"}},
    ]
    r._validate_signal_source_consistency()


def test_signal_source_consistency_rejects_bundle_mismatch(monkeypatch) -> None:
    """影子与上游 bundle 不一致 → raise."""
    import importlib
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "sim", "name": "QMT_SIM_a", "setting": dict(r.QMT_SIM_BASE_SETTING)},
        {"kind": "sim", "name": "QMT_SIM_b", "setting": dict(r.QMT_SIM_BASE_SETTING)},
    ]
    r.STRATEGIES = [
        {"strategy_name": "live", "strategy_class": "X", "gateway_name": "QMT_SIM_a",
         "setting_override": {"bundle_dir": "/tmp/b1", "topk": 7, "n_drop": 1, "trigger_time": "21:00"}},
        {"strategy_name": "shadow", "strategy_class": "X", "gateway_name": "QMT_SIM_b",
         "setting_override": {"bundle_dir": "/tmp/b2",  # 不一致
                              "topk": 7, "n_drop": 1, "signal_source_strategy": "live"}},
    ]
    with pytest.raises(ValueError, match="bundle_dir"):
        r._validate_signal_source_consistency()


def test_signal_source_consistency_rejects_topk_mismatch() -> None:
    """影子与上游 topk 不一致 → raise."""
    import importlib
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "sim", "name": "QMT_SIM_a", "setting": dict(r.QMT_SIM_BASE_SETTING)},
        {"kind": "sim", "name": "QMT_SIM_b", "setting": dict(r.QMT_SIM_BASE_SETTING)},
    ]
    r.STRATEGIES = [
        {"strategy_name": "live", "strategy_class": "X", "gateway_name": "QMT_SIM_a",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1, "trigger_time": "21:00"}},
        {"strategy_name": "shadow", "strategy_class": "X", "gateway_name": "QMT_SIM_b",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 5,  # 不一致
                              "n_drop": 1, "signal_source_strategy": "live"}},
    ]
    with pytest.raises(ValueError, match="topk"):
        r._validate_signal_source_consistency()


def test_signal_source_consistency_rejects_chain_dependency() -> None:
    """影子链式依赖 (影子的上游也是影子) → raise."""
    import importlib
    import run_ml_headless as r
    importlib.reload(r)

    r.GATEWAYS = [
        {"kind": "sim", "name": "QMT_SIM_a", "setting": dict(r.QMT_SIM_BASE_SETTING)},
        {"kind": "sim", "name": "QMT_SIM_b", "setting": dict(r.QMT_SIM_BASE_SETTING)},
        {"kind": "sim", "name": "QMT_SIM_c", "setting": dict(r.QMT_SIM_BASE_SETTING)},
    ]
    r.STRATEGIES = [
        {"strategy_name": "live", "strategy_class": "X", "gateway_name": "QMT_SIM_a",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1, "trigger_time": "21:00"}},
        {"strategy_name": "shadow1", "strategy_class": "X", "gateway_name": "QMT_SIM_b",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1,
                              "signal_source_strategy": "live"}},
        {"strategy_name": "shadow2", "strategy_class": "X", "gateway_name": "QMT_SIM_c",
         "setting_override": {"bundle_dir": "/tmp/b", "topk": 7, "n_drop": 1,
                              "signal_source_strategy": "shadow1"}},  # 链式: 上游 shadow1 也是影子
    ]
    with pytest.raises(ValueError, match="链式"):
        r._validate_signal_source_consistency()


# ============================================================================
# R8: 信号同步 (signal_source_strategy) 字节级等价
#    与 test_signal_source_strategy.py 互补 — 那边是 helper 单测,
#    这边是 "影子策略产物 hash == 上游产物 hash" 的端到端断言.
# ============================================================================

def test_signal_source_byte_equal(tmp_path: Path) -> None:
    """V2 核心断言: 影子策略 link 后 selections.parquet 与上游字节级相等."""
    from vnpy_ml_strategy.template import MLStrategyTemplate

    upstream_name = "csi300_live"
    shadow_name = "csi300_live_shadow"
    days = [date(2026, 4, 28), date(2026, 4, 29), date(2026, 4, 30)]

    # 模拟上游每日产出 selections.parquet (内容固定但日间不同)
    for day in days:
        d = tmp_path / upstream_name / day.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        (d / "selections.parquet").write_text(
            f"upstream_data_{day.isoformat()}", encoding="utf-8",
        )
        (d / "diagnostics.json").write_text(
            f'{{"status":"ok","trade_date":"{day.isoformat()}"}}', encoding="utf-8",
        )

    stub = MLStrategyTemplate.__new__(MLStrategyTemplate)
    stub.strategy_name = shadow_name
    stub.output_root = str(tmp_path)
    stub.signal_source_strategy = upstream_name
    stub.last_status = ""
    stub.write_log = lambda msg: None

    # 逐日 link
    for day in days:
        stub._link_selections_from_upstream(day)

    # 字节级等价断言
    import hashlib
    for day in days:
        day_str = day.strftime("%Y%m%d")
        u = (tmp_path / upstream_name / day_str / "selections.parquet").read_bytes()
        s = (tmp_path / shadow_name / day_str / "selections.parquet").read_bytes()
        assert hashlib.md5(u).hexdigest() == hashlib.md5(s).hexdigest(), \
            f"day {day} selections.parquet 字节不等"


# ============================================================================
# R5/R6 综合: FakeQmtGateway 实例化 + 命名校验 + Phase 4 enable_auto_settle
# ============================================================================

def test_fake_qmt_gateway_drop_in_replaceable() -> None:
    """FakeQmtGateway 可作为 QmtGateway 的接口替身 (zero-prod-risk)."""
    from vnpy.event import EventEngine
    from vnpy_ml_strategy.test.fakes.fake_qmt_gateway import FakeQmtGateway

    ee = EventEngine()
    gw = FakeQmtGateway(ee, "QMT")
    try:
        # 命名 = QMT, 内部为 sim 撮合 → 双轨架构里替代 live gateway 用
        assert gw.gateway_name == "QMT"
        assert hasattr(gw, "md")
        assert hasattr(gw, "td")
        assert gw._auto_settle_enabled is True

        # Phase 4 回放支持: enable_auto_settle 必须可调
        gw.enable_auto_settle(False)
        assert gw._auto_settle_enabled is False
        gw.enable_auto_settle(True)
        assert gw._auto_settle_enabled is True
    finally:
        # ee 没 start, 不需 stop
        pass
