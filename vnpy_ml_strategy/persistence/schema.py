"""Result persistence column / field constants.

子进程(qlib_strategy_core.cli.run_inference) 已经写好 predictions.parquet
和 metrics.json; 本模块定义**主进程侧**还需额外落盘的 selections / orders
schema.
"""

from __future__ import annotations

# selections.parquet columns
COL_TRADE_DATE = "trade_date"
COL_INSTRUMENT = "instrument"
COL_SCORE = "score"
COL_RANK = "rank"
COL_WEIGHT = "weight"
COL_TARGET_PRICE = "target_price"
COL_SIDE = "side"          # "long" / "short"
COL_MODEL_RUN_ID = "model_run_id"

SELECTION_COLUMNS = [
    COL_TRADE_DATE,
    COL_INSTRUMENT,
    COL_SCORE,
    COL_RANK,
    COL_WEIGHT,
    COL_TARGET_PRICE,
    COL_SIDE,
    COL_MODEL_RUN_ID,
]

# orders.jsonl schema
ORDER_FIELD_INSTRUMENT = "instrument"
ORDER_FIELD_EXCHANGE = "exchange"
ORDER_FIELD_DIRECTION = "direction"   # "long" / "short" / "close"
ORDER_FIELD_OFFSET = "offset"         # "open" / "close" / ""
ORDER_FIELD_PRICE = "price"
ORDER_FIELD_VOLUME = "volume"
ORDER_FIELD_ORDER_TYPE = "order_type"
ORDER_FIELD_STATUS = "status"         # "submitted" / "filtered_out" / "failed"
ORDER_FIELD_FILTER_REASON = "filter_reason"  # "t_plus_1_hold" / "limit_up" / ...
ORDER_FIELD_TIMESTAMP = "timestamp"
