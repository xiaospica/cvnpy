"""``QlibMLStrategy`` — 具体 ML 策略示例, 消费 qlib 训练产出的 bundle.

pipeline:
    1. run_daily_pipeline (父类实现) 调 subprocess 拿 (pred_df, metrics, diag)
    2. select_topk (父类默认) 取分数最高 topk 只
    3. generate_orders (本类实现) 做 T+1 / 涨跌停 / ST 过滤 → send_order
    4. 落盘 selections.parquet + orders.jsonl
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

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

    def generate_orders(
        self,
        pred_score: pd.Series,
        selected: pd.DataFrame,
        on_day: Optional[date] = None,
    ) -> None:
        """Phase 6: 把全量 pred_score 交给 qlib TopkDropoutStrategy 算法决策。

        策略真实时序（实盘）：T 日 21:00 推理 → T+1 日 09:30 开盘 rebalance。
        本方法是实时模式下 ``run_daily_pipeline`` 末尾的入口，委托给父类
        ``rebalance_to_target(pred_score, on_day=on_day)`` 走 qlib TopkDropoutStrategy
        等价算法。

        ``on_day`` 来自 ``run_daily_pipeline`` 的 ``today`` (= as_of_date or date.today()),
        smoke / 历史回放场景下必须透传, 否则会错用 wall-clock today 导致 log 乱日期 +
        参考价取错日.

        回放模式不走本方法，由 ``_replay_loop_iter`` 直接调
        ``rebalance_to_target(prev_day_pred_score, on_day=current_day)``。

        ``selected`` 参数保留用于子类做日志 / 自定义；算法决策只用 pred_score 全量。
        """
        if not self.enable_trading:
            self.write_log("generate_orders: enable_trading=False, skipping (dry-run)")
            return
        self.rebalance_to_target(pred_score, on_day=on_day or date.today())

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
