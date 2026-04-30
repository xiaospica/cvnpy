"""Phase 4：模拟交易加速回放控制器单元测试。

覆盖 plan 文档"Phase 4 验收标准"的核心场景：
  - as_of_date 透传到 run_daily_pipeline
  - 起止日从 bundle task.json 推导
  - setting override 优先于 task.json
  - 实盘 gateway 守门跳过
  - 显式 replay_start_date < test_start 报错
  - 续跑跳过已完成日期
  - gateway 自动 settle 可禁用
  - pause_job 仅暂停本策略
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest


# ---- helpers ----------------------------------------------------------


def _make_task_json(tmp_path: Path, test_start: str, test_end: str = "2026-01-23") -> Path:
    """构造一个最小 bundle 目录，含 task.json。"""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "task.json").write_text(
        json.dumps({
            "dataset": {
                "kwargs": {
                    "segments": {
                        "train": ["2015-01-01", "2021-12-31"],
                        "valid": ["2022-01-01", "2025-12-31"],
                        "test": [test_start, test_end],
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    return bundle


def _make_strategy(bundle_dir: Path, **overrides) -> Any:
    """构造一个最小 MLStrategyTemplate 子类实例（mock signal_engine）。"""
    from vnpy_ml_strategy.template import MLStrategyTemplate

    class _ConcreteStrategy(MLStrategyTemplate):
        # ABC 不要求覆盖 generate_orders（已是普通方法），但子类需要实现
        def generate_orders(self, selected):  # type: ignore[override]
            return None

    signal_engine = MagicMock()
    signal_engine.is_trade_day.return_value = True  # 默认所有日都是交易日
    signal_engine.run_pipeline_now.return_value = True
    signal_engine.scheduler = MagicMock()
    signal_engine.main_engine = MagicMock()

    inst = _ConcreteStrategy(signal_engine, "test_strategy")
    inst.bundle_dir = str(bundle_dir)
    inst.gateway = "QMT_SIM_test"
    inst.output_root = str(bundle_dir.parent / "output")
    inst.enable_replay = True
    for k, v in overrides.items():
        setattr(inst, k, v)
    return inst


# ---- run_pipeline_now as_of_date 透传 --------------------------------


def test_run_pipeline_now_with_as_of_date_overrides_today() -> None:
    """engine.run_pipeline_now(name, as_of_date=day) → scheduler.run_job_now(name, as_of_date=day)
    → wrapped(as_of_date=day) → run_daily_pipeline(as_of_date=day) → today=day
    """
    from vnpy_common.scheduler import DailyTimeTaskScheduler

    sched = DailyTimeTaskScheduler()
    captured = {}

    def fake_pipeline(as_of_date=None):
        captured["as_of"] = as_of_date

    sched.register_daily_job("strat1", "21:00", fake_pipeline)
    sched.run_job_now("strat1", as_of_date=date(2026, 2, 15))
    assert captured["as_of"] == date(2026, 2, 15)

    captured.clear()
    sched.run_job_now("strat1")  # 不传 as_of_date → 默认 None
    assert captured["as_of"] is None


# ---- _resolve_replay_window -------------------------------------------


def test_replay_window_resolved_from_task_json(tmp_path) -> None:
    bundle = _make_task_json(tmp_path, test_start="2026-01-01", test_end="2026-01-23")
    strat = _make_strategy(bundle)
    window = strat._resolve_replay_window()
    assert window is not None
    start, end = window
    assert start == date(2026, 1, 1)
    # end 默认 today-1（相对今日，断言只检查 ≥ start）
    assert end >= start


def test_replay_window_setting_override(tmp_path) -> None:
    """setting 显式给 replay_start_date / replay_end_date 时覆盖 task.json 推导。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(
        bundle,
        replay_start_date="2026-02-15",
        replay_end_date="2026-03-10",
    )
    window = strat._resolve_replay_window()
    assert window == (date(2026, 2, 15), date(2026, 3, 10))


def test_replay_window_disabled_returns_none(tmp_path) -> None:
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, enable_replay=False)
    assert strat._resolve_replay_window() is None


# ---- _start_replay_if_needed -----------------------------------------


def test_replay_skipped_for_non_sim_gateway(tmp_path) -> None:
    """实盘 gateway="QMT" 时 _start_replay_if_needed 立即 return，状态 skipped_live。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, gateway="QMT")
    strat._start_replay_if_needed()
    assert strat.replay_status == "skipped_live"
    # 不应启动线程：signal_engine.run_pipeline_now 不被调
    strat.signal_engine.run_pipeline_now.assert_not_called()


def test_replay_raises_on_explicit_start_before_test_start(tmp_path) -> None:
    """显式 replay_start_date 早于 bundle test_start → _validate_explicit_replay_start raise。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, replay_start_date="2025-06-01")
    with pytest.raises(ValueError, match="历史数据泄漏"):
        strat._validate_explicit_replay_start()


def test_replay_explicit_start_inside_test_passes(tmp_path) -> None:
    """显式 replay_start_date 在 test 区间内 → 校验通过。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, replay_start_date="2026-02-15")
    strat._validate_explicit_replay_start()  # 不 raise


# ---- _replay_loop_body 续跑幂等 ---------------------------------------


def test_replay_skips_batch_predict_when_diagnostics_complete(tmp_path) -> None:
    """Phase A 续跑：所有交易日已有 batch_mode diagnostics → 跳过批量推理（不调 run_inference_range）。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)

    # 制造 4 天（02 03 04 05）的 batch_mode diagnostics + predictions（覆盖整窗口）
    import pandas as pd
    for day_str in ("20260102", "20260103", "20260104", "20260105"):
        out_dir = Path(strat.output_root) / strat.strategy_name / day_str
        out_dir.mkdir(parents=True)
        (out_dir / "diagnostics.json").write_text(
            json.dumps({"status": "ok", "batch_mode": True, "rows": 300}),
            encoding="utf-8",
        )
        idx = pd.MultiIndex.from_tuples(
            [(pd.Timestamp(f"2026-01-{day_str[-2:]}"), "000001.SZ")],
            names=["datetime", "instrument"],
        )
        pd.DataFrame({"score": [1.0]}, index=idx).to_parquet(out_dir / "predictions.parquet")

    fake_gateway = MagicMock()
    # Mock 掉 generate_orders 让它不抛 NotImplementedError
    strat.generate_orders = MagicMock()
    strat._replay_loop_body(date(2026, 1, 2), date(2026, 1, 5), fake_gateway)

    # 跳过批量推理：run_inference_range 不应被调
    strat.signal_engine.run_inference_range.assert_not_called()
    # settle 仍按交易日调（fake _is_trade_day 总返 True，2026-01-02..05 = 4 天）
    assert fake_gateway.td.counter.settle_end_of_day.call_count == 4


def test_replay_calls_batch_predict_when_diagnostics_missing(tmp_path) -> None:
    """新部署：缺 diagnostics → 批量推理触发一次 run_inference_range。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)
    # 模拟批量推理写出 diagnostics 但不写 predictions（空场景）
    strat.signal_engine.run_inference_range.return_value = {
        "n_days_total": 2, "n_days_with_data": 0, "returncode": 0, "stderr_tail": "",
    }

    fake_gateway = MagicMock()
    strat._replay_loop_body(date(2026, 1, 2), date(2026, 1, 3), fake_gateway)

    # 批量推理被调一次
    strat.signal_engine.run_inference_range.assert_called_once()
    call_kwargs = strat.signal_engine.run_inference_range.call_args.kwargs
    assert call_kwargs["range_start"] == date(2026, 1, 2)
    assert call_kwargs["range_end"] == date(2026, 1, 3)
    # settle 仍按日调
    assert fake_gateway.td.counter.settle_end_of_day.call_count == 2


# ---- gateway 自动 settle 守门 ----------------------------------------


def test_gateway_auto_settle_can_be_disabled() -> None:
    """enable_auto_settle(False) 后，process_timer_event 跨日不调 settle。"""
    from vnpy.event import EventEngine
    from vnpy_qmt_sim import QmtSimGateway

    ee = EventEngine()
    ee.start()
    try:
        gw = QmtSimGateway(ee, "QMT_SIM_TEST_AUTO_SETTLE")
        # 不需要 connect 完整 setup，直接操作 timer 检测路径
        gw._last_seen_date = date(2026, 1, 1)
        gw.td.counter.settle_end_of_day = MagicMock()

        gw.enable_auto_settle(False)
        # 模拟 process_timer_event 跨日（用 monkey-patch datetime.now）
        import vnpy_qmt_sim.gateway as gw_mod
        orig_dt = gw_mod.datetime
        try:
            class _FakeDt:
                @classmethod
                def now(cls):
                    class _D:
                        @staticmethod
                        def date():
                            return date(2026, 1, 2)
                    return _D()
            gw_mod.datetime = _FakeDt
            gw.process_timer_event(MagicMock())
        finally:
            gw_mod.datetime = orig_dt

        # 自动 settle 已禁用 → settle_end_of_day 不被调
        gw.td.counter.settle_end_of_day.assert_not_called()
        assert gw._last_seen_date == date(2026, 1, 2)  # _last_seen_date 仍更新
    finally:
        ee.stop()


# ---- pause_job 隔离 ---------------------------------------------------


def test_replay_pause_resume_only_own_cron() -> None:
    """pause_job(strategy_A) 不影响 strategy_B 的 cron next_run_time。"""
    from vnpy_common.scheduler import DailyTimeTaskScheduler

    sched = DailyTimeTaskScheduler()
    sched.register_daily_job("stratA", "21:00", lambda **k: None)
    sched.register_daily_job("stratB", "22:00", lambda **k: None)
    sched.start()
    try:
        next_a_before = sched.get_job_next_run_time("stratA")
        next_b_before = sched.get_job_next_run_time("stratB")
        assert next_a_before is not None and next_b_before is not None

        sched.pause_job("stratA")
        assert sched.get_job_next_run_time("stratA") is None
        # B 不受影响
        assert sched.get_job_next_run_time("stratB") is not None

        sched.resume_job("stratA")
        assert sched.get_job_next_run_time("stratA") is not None
    finally:
        sched.stop(wait=False)
