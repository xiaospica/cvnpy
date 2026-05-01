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


# ---- Phase 5: rebalance_to_target -----------------------------------


def test_instrument_to_vt_conversion(tmp_path) -> None:
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)
    assert strat._instrument_to_vt("000001.SZ") == "000001.SZSE"
    assert strat._instrument_to_vt("600519.SH") == "600519.SSE"
    assert strat._instrument_to_vt("000001.SZSE") == "000001.SZSE"  # 已是 vt
    assert strat._instrument_to_vt("") is None
    assert strat._instrument_to_vt("nodot") is None


def test_calculate_buy_amount_qlib_equiweight(tmp_path) -> None:
    """qlib TopkDropoutStrategy 等权公式：floor(cash × risk / n_buys / price / 100) × 100."""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, risk_degree=0.95)

    # cash=1_000_000, risk=0.95, n_buys=7, price=11.0
    # value = 1_000_000 * 0.95 / 7 = 135_714.28...
    # amount = 135_714.28 / 11.0 = 12_337.66...
    # lots = 12_337 // 100 = 123 → 12_300
    assert strat._calculate_buy_amount(11.0, 1_000_000.0, 7) == 12_300

    # cash=500_000, risk=0.95, n_buys=5, price=50.0
    # value = 500_000 * 0.95 / 5 = 95_000
    # amount = 95_000 / 50.0 = 1900 → 1900
    assert strat._calculate_buy_amount(50.0, 500_000.0, 5) == 1900

    # cash 不够买 1 手
    assert strat._calculate_buy_amount(1500.0, 100_000.0, 7) == 0
    # 边界
    assert strat._calculate_buy_amount(0, 1_000_000.0, 7) == 0
    assert strat._calculate_buy_amount(11.0, 0, 7) == 0
    assert strat._calculate_buy_amount(11.0, 1_000_000.0, 0) == 0


def test_rebalance_uses_qlib_topk_dropout(tmp_path) -> None:
    """Phase 6 sanity: rebalance_to_target 用 topk_dropout_decision 算法决策。

    持仓 [A, B, C] vs pred top-3 [A, B, D]; n_drop=1 → 卖 C 买 D（不全卖）。
    详细分支等价见 test_topk_dropout_decision.py。
    """
    import pandas as pd
    from vnpy.trader.constant import Direction as _Dir
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, risk_degree=0.95)
    strat.topk = 3
    strat.n_drop = 1
    strat.trading = True

    # 当前持仓 A, B, C (ts → vt: A.SZ → A.SZSE, etc.)
    positions = []
    for code in ["000001.SZ", "000002.SZ", "000003.SZ"]:
        vt = code[:-3] + ".SZSE"
        pos = MagicMock()
        pos.gateway_name = strat.gateway
        pos.direction = _Dir.LONG
        pos.volume = 100
        pos.yd_volume = 100
        pos.vt_symbol = vt
        positions.append(pos)
    strat.signal_engine.main_engine.get_all_positions.return_value = positions

    acc = MagicMock()
    acc.gateway_name = strat.gateway
    acc.balance = 1_000_000.0
    acc.frozen = 0.0
    strat.signal_engine.main_engine.get_all_accounts.return_value = [acc]

    # md.get_full_tick: 所有股票都可交易，价格 10.0
    def fake_get_full_tick(vt):
        tick = MagicMock(spec=["last_price", "pre_close", "limit_up", "limit_down"])
        tick.last_price = 10.0
        tick.pre_close = 10.0
        tick.limit_up = 11.0
        tick.limit_down = 9.0
        return tick
    fake_md = MagicMock()
    fake_md.get_full_tick = fake_get_full_tick
    fake_gw = MagicMock()
    fake_gw.md = fake_md
    fake_gw.gateway_name = strat.gateway
    strat.signal_engine.main_engine.get_gateway = lambda name: fake_gw if name == strat.gateway else None

    sent = []
    strat.send_order = lambda **kw: sent.append(kw) or ["mock"]

    # 全量 pred_score: A(0.9) > B(0.7) > D(0.5) > C(0.3) > E(0.1)
    pred_score = pd.Series({
        "000001.SZ": 0.9,  # A — 持仓
        "000002.SZ": 0.7,  # B — 持仓
        "000004.SZ": 0.5,  # D — 新候选
        "000003.SZ": 0.3,  # C — 持仓
        "000005.SZ": 0.1,  # E
    })
    stats = strat.rebalance_to_target(pred_score, on_day=date(2026, 1, 5))

    # qlib TopkDropoutStrategy n_drop=1 应该卖 C 买 D
    sells = [s for s in sent if s["direction"].value == "空"]
    buys = [s for s in sent if s["direction"].value == "多"]
    assert len(sells) == 1, f"应卖 1 只 (C)；实际 sent: {sent}"
    assert sells[0]["vt_symbol"] == "000003.SZSE"  # C
    assert len(buys) == 1, f"应买 1 只 (D)；实际 sent: {sent}"
    assert buys[0]["vt_symbol"] == "000004.SZSE"  # D


def test_rebalance_empty_pred_skips(tmp_path) -> None:
    """空 pred_score → 不调仓"""
    import pandas as pd
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)
    strat.trading = True
    strat.signal_engine.main_engine.get_all_positions.return_value = []
    sent = []
    strat.send_order = lambda **kw: sent.append(kw) or ["mock"]
    stats = strat.rebalance_to_target(pd.Series(dtype=float), on_day=date(2026, 1, 5))
    assert stats["sells_dispatched"] == 0
    assert stats["buys_dispatched"] == 0
    assert len(sent) == 0


def test_rebalance_first_day_no_holdings(tmp_path) -> None:
    """空仓首日 → 按 pred top-k 全买入"""
    import pandas as pd
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle, risk_degree=0.95)
    strat.topk = 3
    strat.n_drop = 1
    strat.trading = True
    strat.signal_engine.main_engine.get_all_positions.return_value = []

    acc = MagicMock()
    acc.gateway_name = strat.gateway
    acc.balance = 1_000_000.0
    acc.frozen = 0.0
    strat.signal_engine.main_engine.get_all_accounts.return_value = [acc]

    def fake_get_full_tick(vt):
        tick = MagicMock(spec=["last_price", "pre_close", "limit_up", "limit_down"])
        tick.last_price = 10.0
        tick.pre_close = 10.0
        tick.limit_up = 11.0
        tick.limit_down = 9.0
        return tick
    fake_md = MagicMock()
    fake_md.get_full_tick = fake_get_full_tick
    fake_gw = MagicMock()
    fake_gw.md = fake_md
    fake_gw.gateway_name = strat.gateway
    strat.signal_engine.main_engine.get_gateway = lambda name: fake_gw if name == strat.gateway else None

    sent = []
    strat.send_order = lambda **kw: sent.append(kw) or ["mock"]

    pred_score = pd.Series({
        "000001.SZ": 0.9, "000002.SZ": 0.8, "000003.SZ": 0.7,
        "000004.SZ": 0.6, "000005.SZ": 0.5,
    })
    stats = strat.rebalance_to_target(pred_score, on_day=date(2026, 1, 5))
    # qlib 算法首日: last=[], today=top(n_drop+topk-len(last)=4)=[A,B,C,D]
    # comb sort = ABCDE, 末 1 = [E] (E 不在 last → sell=空)
    # buy = today[: 0+3-0=3] = [A, B, C]
    buys = [s for s in sent if s["direction"].value == "多"]
    assert len(buys) == 3
    bought = sorted(b["vt_symbol"] for b in buys)
    assert bought == ["000001.SZSE", "000002.SZSE", "000003.SZSE"]


def test_get_long_positions_recognizes_direction_enum(tmp_path) -> None:
    """关键回归：_get_long_positions 必须正确识别 Direction.LONG 枚举（vnpy gateway 推 enum）。

    旧 bug：用 str(pos.direction) != Direction.LONG.value 比较 →
    str(Direction.LONG) = "Direction.LONG" 永远 ≠ "多" → 全部 continue → 空 dict。
    现场症状：rebalance 每天报 current=0，无论实际有多少持仓。
    """
    from vnpy.trader.constant import Direction as _Dir
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)

    pos = MagicMock()
    pos.gateway_name = strat.gateway
    pos.direction = _Dir.LONG  # 真实 enum，不是 string
    pos.volume = 100
    pos.yd_volume = 100
    pos.vt_symbol = "000001.SZSE"
    strat.signal_engine.main_engine.get_all_positions.return_value = [pos]

    result = strat._get_long_positions()
    assert "000001.SZSE" in result, (
        "_get_long_positions 应识别 Direction.LONG enum；旧 bug 用 str(enum) 比较 .value 永远不匹配"
    )


def test_get_reference_price_reads_from_md_cache_not_oms(tmp_path) -> None:
    """关键回归：_get_reference_price 优先读 gateway.md.get_full_tick 而非 main_engine.get_tick。

    回放期间 md.refresh_tick 只写 _tick_cache 不调 gateway.on_tick → vnpy OMS 永远空。
    若读 main_engine.get_tick 则永远返 None → "无参考价" → 0 买入（已发生过的 bug）。
    """
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)

    # md 缓存有 tick (last_price=12.34)
    md_tick = MagicMock(spec=["last_price", "pre_close"])
    md_tick.last_price = 12.34
    md_tick.pre_close = 11.50
    fake_md = MagicMock()
    fake_md.get_full_tick = lambda vt: md_tick if vt == "000001.SZSE" else None
    fake_gw = MagicMock()
    fake_gw.md = fake_md
    fake_gw.gateway_name = strat.gateway
    strat.signal_engine.main_engine.get_gateway = lambda name: fake_gw if name == strat.gateway else None

    # main_engine.get_tick 返 None（模拟 OMS 没拿到 tick — 回放真实场景）
    strat.signal_engine.main_engine.get_tick.return_value = None

    assert strat._get_reference_price("000001.SZSE") == 12.34


def test_refresh_market_data_for_day_includes_candidates(tmp_path) -> None:
    """_refresh_market_data_for_day 必须刷新候选股 tick，否则新候选 tick.last_price 还是初始值。"""
    bundle = _make_task_json(tmp_path, test_start="2026-01-01")
    strat = _make_strategy(bundle)
    strat.signal_engine.main_engine.get_all_positions.return_value = []  # 无持仓

    # mock gateway.md.refresh_tick 记录被刷的 vt_symbols
    refreshed: list[tuple[str, date]] = []
    fake_md = MagicMock()
    fake_md.refresh_tick = lambda vt, as_of_date=None: refreshed.append((vt, as_of_date))
    fake_gw = MagicMock()
    fake_gw.md = fake_md
    fake_gw.gateway_name = strat.gateway
    strat.signal_engine.main_engine.get_gateway = lambda name: fake_gw if name == strat.gateway else None

    day = date(2026, 1, 5)
    strat._refresh_market_data_for_day(day, candidates=["000002.SZSE", "600519.SSE"])

    refreshed_vts = {r[0] for r in refreshed}
    assert "000002.SZSE" in refreshed_vts
    assert "600519.SSE" in refreshed_vts
    # 全部用 day 作 as_of_date
    for _, as_of in refreshed:
        assert as_of == day


# ---- Phase 5: 撮合层取 tick 价 ---------------------------------------


def test_market_order_uses_tick_price_not_hardcoded() -> None:
    """vnpy_qmt_sim 撮合 market 单时 trade.price = tick.last_price，不再是 10.0。"""
    from vnpy.event import EventEngine
    from vnpy.trader.constant import Direction, Offset, OrderType, Status
    from vnpy.trader.object import OrderData, OrderRequest
    from vnpy_qmt_sim import QmtSimGateway

    ee = EventEngine()
    ee.start()
    try:
        gw = QmtSimGateway(ee, "QMT_SIM_TEST_TRADE_PRICE")
        # 注入一个 tick.last_price = 25.5（不是 10.0）
        from vnpy.trader.object import TickData
        from vnpy.trader.constant import Exchange
        from datetime import datetime
        gw.md._tick_cache["000001.SZSE"] = TickData(
            symbol="000001",
            exchange=Exchange.SZSE,
            datetime=datetime.now(),
            name="平安银行",
            last_price=25.5,
            limit_up=28.05,
            limit_down=22.95,
            gateway_name=gw.gateway_name,
        )

        # 模拟一个市价 LONG 订单走 _execute_trade 路径
        order = OrderData(
            symbol="000001",
            exchange=Exchange.SZSE,
            orderid="1",
            type=OrderType.MARKET,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=0.0,  # MARKET 单 price=0
            volume=100,
            traded=0,
            status=Status.NOTTRADED,
            datetime=datetime.now(),
            gateway_name=gw.gateway_name,
        )
        gw.td.counter.orders["1"] = order
        gw.td.counter._execute_trade(order, 100)

        # 应该有一笔成交，价格 = 25.5（不是 10.0）
        trades = list(gw.td.counter.trades.values())
        assert len(trades) == 1
        assert trades[0].price == 25.5
    finally:
        ee.stop()
