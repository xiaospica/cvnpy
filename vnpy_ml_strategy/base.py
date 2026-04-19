"""vnpy_ml_strategy 常量 / 事件类型 / 枚举.

事件类型遵循 vnpy 规范: 以 ``e`` 开头, 点号分隔层级 (``eMlStrategy.csi300_lgb``).
订阅方可用 ``eMlStrategy.``(结尾点号) 做通配符.
"""

from __future__ import annotations

from enum import Enum


APP_NAME = "MlStrategy"


# ---------------------------------------------------------------------------
# 事件类型 (webtrader 适配器监听这些)
# ---------------------------------------------------------------------------


EVENT_ML_METRICS = "eMlMetrics."        # 单日监控指标就绪, payload=dict
EVENT_ML_PREDICTION = "eMlPrediction."  # 预测分数就绪, payload=dict (含 topk)
EVENT_ML_FAILED = "eMlFailed."          # 子进程运行失败/超时
EVENT_ML_EMPTY = "eMlEmpty."            # 子进程跑完但无有效预测 (例如未发现数据)
EVENT_ML_HEARTBEAT = "eMlHeartbeat."    # 主进程周期心跳, 不依赖子进程存活
EVENT_ML_LOG = "eMlLog."                # 结构化日志转发
EVENT_ML_STRATEGY = "eMlStrategy"       # 策略实例状态 (init/start/stop 后发, UI 消费)


# ---------------------------------------------------------------------------
# pipeline 阶段枚举 — 用于日志结构化
# ---------------------------------------------------------------------------


class Stage(str, Enum):
    """run_daily_pipeline 各阶段标识."""

    FETCH = "fetch"
    PREPROCESS = "preprocess"
    PREDICT = "predict"
    SELECT = "select"
    ORDER = "order"
    SAVE = "save"
    METRICS = "metrics"
    PUBLISH = "publish"


# ---------------------------------------------------------------------------
# 子进程诊断状态 (与 qlib_strategy_core.cli.run_inference diagnostics.json 对齐)
# ---------------------------------------------------------------------------


class InferenceStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    FAILED = "failed"


# 子进程 schema_version 主进程必须兼容的主版本号
DIAGNOSTICS_SCHEMA_MAJOR = 1
