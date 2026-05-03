"""调试: 用真实 vnpy_qmt_sim.SimulationCounter 重放到 02-24 EOD, dump per-stock cost.

目的: 对比 mlearnweb db 02-24 EOD 总值 (1,090,717) 与重放计算值 (1,072,371) 的 +18k 差异
来自哪个 stock 的哪个 cost 分量。

不依赖 vnpy 主流程 — 直接构造 EventEngine + QmtSimGateway + SimulationCounter,
喂入 sim_trades 历史 + 调 settle_end_of_day。
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]  # vnpy_strategy_dev
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor" / "qlib_strategy_core"))

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.object import OrderRequest, TradeData, OrderData

from vnpy_qmt_sim import QmtSimGateway

SIM_DB = Path(r"F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state/sim_QMT_SIM_csi300.db")


def _vt2symbol_exchange(vt: str):
    sym, ex = vt.rsplit(".", 1)
    return sym, Exchange(ex)


def main():
    ee = EventEngine()
    ee.start()
    gw = QmtSimGateway(ee, "QMT_SIM_csi300")
    setting = {
        "模拟资金": 1_000_000.0,
        "部分成交率": 0.0,
        "拒单率": 0.0,
        "订单超时秒数": 30,
        "成交延迟毫秒": 0,
        "报单上报延迟毫秒": 0,
        "卖出持仓不足拒单": "是",
        "行情源": "merged_parquet",
        "merged_parquet_merged_root": r"D:/vnpy_data/snapshots/merged",
        "merged_parquet_reference_kind": "today_open",
        "merged_parquet_fallback_days": 10,
        "merged_parquet_stale_warn_hours": 48,
        # 不挂 persistence — 直接喂内存 trades, 防止持久化干扰
        "启用持久化": "否",
    }
    gw.connect(setting)

    counter = gw.td.counter

    # Read trades
    conn = sqlite3.connect(str(SIM_DB))
    cur = conn.cursor()
    cur.execute("SELECT vt_symbol, direction, volume, price, datetime FROM sim_trades WHERE datetime <= '2026-02-25T00:00:00' ORDER BY datetime")
    rows = cur.fetchall()
    conn.close()

    by_day = defaultdict(list)
    for vt, dir_str, vol, p, dt_str in rows:
        d = datetime.fromisoformat(dt_str).date()
        by_day[d].append((vt, dir_str, float(vol), float(p), dt_str))

    last_day = sorted(by_day)[-1]
    seq = 1
    for d in sorted(by_day):
        # 设回放逻辑日 (让 update_position 等方法看到 _replay_now)
        counter._replay_now = datetime.combine(d, dtime(9, 30, 0))

        # 喂 trades 直接通过 update_position + update_account (绕过 OrderRequest)
        for vt, dir_str, vol, p, dt_str in by_day[d]:
            sym, ex = _vt2symbol_exchange(vt)
            direction = Direction.LONG if "多" in dir_str else Direction.SHORT
            offset = Offset.OPEN if direction == Direction.LONG else Offset.CLOSE

            orderid = f"replay-{seq}"
            seq += 1
            order = OrderData(
                symbol=sym, exchange=ex, orderid=orderid,
                type=OrderType.MARKET, direction=direction, offset=offset,
                price=p, volume=vol, traded=vol,
                status=Status.ALLTRADED,
                gateway_name="QMT_SIM_csi300",
            )
            counter.orders[orderid] = order

            trade = TradeData(
                symbol=sym, exchange=ex, orderid=orderid,
                tradeid=f"t-{seq}", direction=direction, offset=offset,
                price=p, volume=vol,
                datetime=datetime.fromisoformat(dt_str),
                gateway_name="QMT_SIM_csi300",
            )
            # 模拟卖出冻结 (sell 才需要 — counter._execute_trade 走的路径)
            counter.update_position(trade)
            counter.update_account(trade)

        # Refresh ticks for all positions so settle's get_quote 拿到当日数据
        symbols_to_refresh = set()
        for pos in counter.positions.values():
            if pos.volume > 0:
                symbols_to_refresh.add(pos.vt_symbol)
        for vt in symbols_to_refresh:
            try:
                gw.md.refresh_tick(vt, as_of_date=d)
            except Exception as e:
                print(f"  refresh fail {vt}@{d}: {e}")

        # Settle
        counter.settle_end_of_day(d)

        if d == last_day:
            print(f"\n=== 真实 vnpy_qmt_sim {d} EOD ===")
            print(f"  capital: {counter.capital:.2f}  frozen: {counter.frozen:.2f}")
            total_mv = 0.0
            for vt, pos in sorted(counter.positions.items()):
                if pos.volume <= 0:
                    continue
                mv = pos.volume * pos.price
                total_mv += mv
                print(f"  {pos.vt_symbol}: vol={pos.volume} cost={pos.price:.4f} mv={mv:.0f}")
            equity = counter.capital - counter.frozen + total_mv
            print(f"  TOTAL: {equity:.2f}")
            print(f"  (mlearnweb db expected: 1090717.37)")

    ee.stop()


if __name__ == "__main__":
    main()
