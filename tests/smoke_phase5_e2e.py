"""Phase 5 端到端冒烟：验证 _get_reference_price 走 md._tick_cache 后能拿到当日 open
而非 main_engine.get_tick (vnpy OMS) 永远返 None 的旧 bug。

用真实 daily_merged_20260422.parquet 数据，构造完整 QmtSimGateway → md.refresh_tick →
template._get_reference_price → 验证返回 = 当日 open（与 daily_merged 列直接读对比）。

使用独立 gateway_name (QMT_SIM_phase5_smoke)，独立 .trading_state/ 子目录，不影响
用户当前运行的 QMT_SIM_csi300 持久化。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

# 让本脚本脱机运行（直接 python 调）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from vnpy.event import EventEngine
from vnpy_qmt_sim import QmtSimGateway


def main() -> int:
    daily_merged = Path(r"D:/vnpy_data/snapshots/merged/daily_merged_20260422.parquet")
    if not daily_merged.exists():
        print(f"FAIL: {daily_merged} 不存在，无法跑端到端验证")
        return 1

    df = pd.read_parquet(daily_merged)
    df_idx = df.set_index(["ts_code", "trade_date"]).sort_index()
    sample_ts = "000001.SZ"
    if sample_ts not in df_idx.index.get_level_values("ts_code"):
        print(f"FAIL: {sample_ts} 不在 daily_merged 里")
        return 1
    rows_for_ts = df_idx.loc[sample_ts]
    target_date = pd.Timestamp("2026-04-22")
    expected_open = float(rows_for_ts.loc[target_date, "open"])
    print(f"daily_merged: {sample_ts}@2026-04-22 open = {expected_open}")

    # 独立 .trading_state 目录
    # ignore_cleanup_errors: Windows 上 SQLite WAL 文件可能滞留几毫秒
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        ee = EventEngine()
        ee.start()
        try:
            gw = QmtSimGateway(ee, "QMT_SIM_phase5smoke")
            gw.connect({
                "账户": "phase5_smoke",
                "模拟资金": 1_000_000.0,
                "行情源": "merged_parquet",
                "merged_parquet_merged_root": r"D:/vnpy_data/snapshots/merged",
                "merged_parquet_reference_kind": "today_open",
                "merged_parquet_fallback_days": 5,
                "merged_parquet_stale_warn_hours": 48,
                "启用持久化": "是",
                "持久化目录": tmp_dir,
            })

            # 模拟 _refresh_market_data_for_day(day, candidates=[vt])
            vt = "000001.SZSE"
            tick = gw.md.refresh_tick(vt, as_of_date=date(2026, 4, 22))
            assert tick is not None, "refresh_tick 返 None"
            print(f"refresh_tick: tick.last_price = {tick.last_price}")

            if abs(tick.last_price - expected_open) > 1e-6:
                print(f"FAIL: tick.last_price ({tick.last_price}) ≠ daily_merged open ({expected_open})")
                return 1

            # 验证 template._get_reference_price 走 md 缓存
            from vnpy_ml_strategy.template import MLStrategyTemplate

            class _S(MLStrategyTemplate):
                def generate_orders(self, selected):
                    return None

            sig = MagicMock()
            # main_engine.get_gateway 返真实 gateway；get_tick 返 None（模拟 OMS 没值）
            sig.main_engine.get_gateway = lambda name: gw if name == "QMT_SIM_phase5smoke" else None
            sig.main_engine.get_tick = lambda vt: None

            strat = _S(sig, "smoke")
            strat.gateway = "QMT_SIM_phase5smoke"

            ref = strat._get_reference_price(vt)
            print(f"_get_reference_price → {ref}")
            if ref is None or abs(ref - expected_open) > 1e-6:
                print(f"FAIL: _get_reference_price 返 {ref}，预期 {expected_open}")
                return 1

            # 进一步：发市价 LONG 单走撮合，验证 trade.price = today_open
            from vnpy.trader.constant import Direction, Offset, OrderType, Exchange
            from vnpy.trader.object import OrderRequest
            req = OrderRequest(
                symbol="000001",
                exchange=Exchange.SZSE,
                direction=Direction.LONG,
                type=OrderType.MARKET,
                price=0.0,
                volume=100,
                offset=Offset.OPEN,
                reference="smoke",
            )
            vt_orderid = gw.send_order(req)
            print(f"send_order → {vt_orderid}")

            # 触发撮合（_execute_trade 在 send_order 内同步走，但需 process_simulation 推动）
            import time
            time.sleep(1)

            # 找成交
            trades = list(gw.td.counter.trades.values())
            print(f"trades: {[(t.symbol, t.price, t.volume) for t in trades]}")
            if not trades:
                print("FAIL: 没产出 trade（撮合失败）")
                return 1
            t = trades[0]
            if abs(t.price - expected_open) > 1e-6:
                print(f"FAIL: trade.price ({t.price}) ≠ daily_merged open ({expected_open})")
                return 1

            print("PASS: 全部断言通过")
            print(f"   - daily_merged 当日原始 open = {expected_open}")
            print(f"   - tick.last_price = {tick.last_price}")
            print(f"   - _get_reference_price = {ref}")
            print(f"   - trade.price = {t.price}")
            return 0
        finally:
            ee.stop()


if __name__ == "__main__":
    sys.exit(main())
