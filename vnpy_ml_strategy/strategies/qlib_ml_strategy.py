"""``QlibMLStrategy`` — 具体 ML 策略示例, 消费 qlib 训练产出的 bundle.

pipeline:
    1. run_daily_pipeline (父类实现) 调 subprocess 拿 (pred_df, metrics, diag)
    2. select_topk (父类默认) 取分数最高 topk 只
    3. generate_orders (本类实现) 做 T+1 / 涨跌停 / ST 过滤 → send_order
    4. 落盘 selections.parquet + orders.jsonl
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List

import pandas as pd

from vnpy.trader.constant import Direction, Offset, OrderType

from ..base import Stage
from ..persistence.result_store import ResultStore
from ..persistence.schema import (
    COL_INSTRUMENT,
    COL_MODEL_RUN_ID,
    COL_RANK,
    COL_SCORE,
    COL_SIDE,
    COL_TARGET_PRICE,
    COL_TRADE_DATE,
    COL_WEIGHT,
    ORDER_FIELD_DIRECTION,
    ORDER_FIELD_EXCHANGE,
    ORDER_FIELD_FILTER_REASON,
    ORDER_FIELD_INSTRUMENT,
    ORDER_FIELD_OFFSET,
    ORDER_FIELD_ORDER_TYPE,
    ORDER_FIELD_PRICE,
    ORDER_FIELD_STATUS,
    ORDER_FIELD_VOLUME,
)
from ..template import MLStrategyTemplate


class QlibMLStrategy(MLStrategyTemplate):
    """qlib + LightGBM 日频策略 (CSI300 TopK)."""

    author = "ml-team"

    # ------------------------------------------------------------------
    # 选股 — 复用父类默认 (score 降序取 topk)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 下单 — A 股 T+1 + 涨跌停 + ST 过滤
    # ------------------------------------------------------------------

    def generate_orders(self, selected: pd.DataFrame) -> None:
        """把 selected 转成 send_order 调用. 当 enable_trading=False 只落盘不下单.

        过滤规则 (A 股实盘):
          1. **一字涨停**: last tick limit_up == last_price → 过滤买入
          2. **T+1**: 当日新买入的仓位不可卖出 (yd_volume=0 时)
          3. **ST / 停牌**: universe 在子进程 preprocess 已处理, 这里兜底再检查一次
          4. **涨跌停买入方向**: 买入时若涨停, 无法成交 → 过滤
        """
        if selected is None or selected.empty:
            return

        today = date.today()
        trade_date_str = today.strftime("%Y-%m-%d")
        store = ResultStore(self.output_root)

        # 1. 选股落盘 (无论是否下单都记录)
        sel_df = selected.reset_index().copy()
        # 确保列名齐备
        sel_df[COL_TRADE_DATE] = trade_date_str
        sel_df[COL_INSTRUMENT] = sel_df.get("instrument", sel_df.iloc[:, 0])
        sel_df[COL_RANK] = range(1, len(sel_df) + 1)
        sel_df[COL_WEIGHT] = 1.0 / len(sel_df)  # 等权
        sel_df[COL_TARGET_PRICE] = float("nan")  # 市价单
        sel_df[COL_SIDE] = "long"
        sel_df[COL_MODEL_RUN_ID] = self.last_model_run_id

        store.write_selections(self.strategy_name, today, sel_df)

        # 2. 生成 order intents + 过滤
        order_logs: List[Dict[str, Any]] = []
        for rank, (instrument, row) in enumerate(selected.iterrows(), start=1):
            vt_symbol = self._to_vt_symbol(str(instrument))
            log: Dict[str, Any] = {
                ORDER_FIELD_INSTRUMENT: str(instrument),
                ORDER_FIELD_EXCHANGE: vt_symbol.split(".")[-1] if "." in vt_symbol else "",
                ORDER_FIELD_DIRECTION: "long",
                ORDER_FIELD_OFFSET: "open",
                ORDER_FIELD_PRICE: 0.0,
                ORDER_FIELD_VOLUME: self._compute_volume(row),
                ORDER_FIELD_ORDER_TYPE: "market",
                ORDER_FIELD_STATUS: "pending",
                ORDER_FIELD_FILTER_REASON: "",
            }

            # 过滤: 涨停一字板
            if self._is_limit_up(vt_symbol):
                log[ORDER_FIELD_STATUS] = "filtered_out"
                log[ORDER_FIELD_FILTER_REASON] = "limit_up"
                order_logs.append(log)
                continue

            # TODO: ST / 停牌兜底 check (Phase 2.7 补)

            # 实际下单
            if self.enable_trading:
                try:
                    self.send_order(
                        vt_symbol=vt_symbol,
                        direction=Direction.LONG,
                        offset=Offset.OPEN,
                        price=0.0,
                        volume=log[ORDER_FIELD_VOLUME],
                        order_type=OrderType.MARKET,
                    )
                    log[ORDER_FIELD_STATUS] = "submitted"
                except Exception as exc:
                    log[ORDER_FIELD_STATUS] = "failed"
                    log[ORDER_FIELD_FILTER_REASON] = f"{type(exc).__name__}: {exc}"
            else:
                log[ORDER_FIELD_STATUS] = "dry_run"
            order_logs.append(log)

        # 3. 订单落盘 (成交 / 过滤 / 失败 / 干跑 都记)
        store.append_orders(self.strategy_name, today, order_logs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _to_vt_symbol(self, instrument: str) -> str:
        """000408.SZ → 000408.SZSE, 600233.SH → 600233.SSE (vnpy 命名约定)."""
        if instrument.endswith(".SZ"):
            return instrument[:-3] + ".SZSE"
        if instrument.endswith(".SH"):
            return instrument[:-3] + ".SSE"
        return instrument

    def _compute_volume(self, row: pd.Series) -> int:
        """按 cash_per_order 估算手数. 实际价格取不到时简化为 100 股.

        生产实现应查实时行情 last_price 或 ref price.
        """
        # TODO: 在 Phase 2.7 接 vnpy 实时行情拿 last_price 再算精确手数
        return 100

    def _is_limit_up(self, vt_symbol: str) -> bool:
        """查最新 tick 是否涨停一字板.

        Phase 2.3 占位: 总是返回 False (不过滤). Phase 2.7 接 vnpy
        ``self.main_engine.get_tick(vt_symbol)`` 取 limit_up / last_price 比对.
        """
        return False
