"""vnpy 网关与节点的命名约定权威 validator。

# 命名约定

## gateway_name

| 命名模式                       | 示例                              | 含义                          | 网关类             |
|--------------------------------|-----------------------------------|-------------------------------|--------------------|
| `QMT_SIM_<sandbox_id>`         | `QMT_SIM_csi300`, `QMT_SIM_zz500` | 模拟柜台（本地撮合，零下单） | `QmtSimGateway`    |
| `QMT_SIM`                      | `QMT_SIM`                         | 模拟柜台（早期单实例兼容）   | `QmtSimGateway`    |
| `QMT`                          | `QMT`                             | 实盘 miniqmt（连真实券商）   | `QmtGateway`       |
| 其他                           | —                                 | 未知（按节点 mode fallback） | —                  |

判定优先级：
1. `startswith("QMT_SIM")` → "sim"（强制覆盖节点默认）
2. 等于 "QMT" → "live"（强制覆盖）
3. 不匹配 → "unknown"（mlearnweb 推断时 fallback 到节点 mode）

## 节点 mode（vnpy_nodes.yaml）

| 值       | 含义                       | 适用场景                                  |
|----------|----------------------------|-------------------------------------------|
| `sim`    | 节点默认跑模拟策略         | 研究机、CI/CD（默认值）                  |
| `live`   | 节点默认跑实盘策略         | 实盘部署机（连 miniqmt）                 |

未配置时取 `sim`（安全偏好：宁可错挂"模拟"，避免错挂"实盘"误导用户）。

# 演进流程（重要）

任何对约定的扩展（新增 gateway 类型、新增 node mode 值）必须：
1. **先**修改本模块常量 + 测试 `tests/test_common_naming.py`
2. **再**修改使用方（QmtSimGateway 构造、run_ml_headless 启动校验、
   mlearnweb 端的等价复制 `mlearnweb/backend/app/services/vnpy/naming.py` 也要同步更新）
3. **测试**：mlearnweb 端的 `test_naming.py` 互校验测试会自动捕获两侧漂移

# 设计：为何放在 vnpy_common 而非 vnpy_qmt_sim

命名约定**同时**约束 `QMT_SIM_*`（vnpy_qmt_sim 网关）和 `QMT`（vnpy_qmt 网关），
属于跨网关包的共享知识。放在 vnpy_qmt_sim 里会让 vnpy_qmt_sim "知道实盘网关名"
→ 反向依赖。vnpy_common 已有定位"跨 app 通用工具包，解开反向依赖"，正合适。
"""
from __future__ import annotations

import re
from typing import Literal, Optional

# 约定 1：gateway_name 模式
_PATTERN_SIM = re.compile(r"^QMT_SIM(_[A-Za-z0-9]+)*$")
_NAME_LIVE = "QMT"

# 约定 2：节点 mode 合法值
VALID_NODE_MODES = ("live", "sim")

GatewayClass = Literal["sim", "live", "unknown"]
NodeMode = Literal["live", "sim"]


def classify_gateway(gateway_name: str) -> GatewayClass:
    """按命名约定分类 gateway_name。

    >>> classify_gateway("QMT_SIM_csi300")
    'sim'
    >>> classify_gateway("QMT_SIM")
    'sim'
    >>> classify_gateway("QMT")
    'live'
    >>> classify_gateway("unknown_gw")
    'unknown'
    >>> classify_gateway("")
    'unknown'

    无副作用，仅返回分类。需要硬校验时调 validate_gateway_name。
    """
    if not isinstance(gateway_name, str):
        return "unknown"
    if _PATTERN_SIM.match(gateway_name):
        return "sim"
    if gateway_name == _NAME_LIVE:
        return "live"
    return "unknown"


def validate_gateway_name(
    gateway_name: str,
    *,
    expected_class: Optional[GatewayClass] = None,
) -> None:
    """严格校验 gateway_name。

    参数
    ----
    expected_class : 给定时还要求分类匹配，防止 ``QmtSimGateway`` 用 ``"QMT"`` 名
                     这种"类与命名错配"的隐蔽 bug。

    抛出
    ----
    ValueError : 命名违反约定，或与 expected_class 不符。

    示例
    ----
    >>> validate_gateway_name("QMT_SIM_csi300", expected_class="sim")  # OK
    >>> validate_gateway_name("QMT", expected_class="live")  # OK
    >>> try:
    ...     validate_gateway_name("BAD_NAME")
    ... except ValueError as e:
    ...     print("rejected")
    rejected
    """
    cls = classify_gateway(gateway_name)
    if cls == "unknown":
        raise ValueError(
            f"gateway_name={gateway_name!r} 违反命名约定。允许：'QMT' (实盘) / "
            f"'QMT_SIM' / 'QMT_SIM_<sandbox_id>' (模拟)。"
            f"详见 vnpy_common/naming.py 模块 docstring。"
        )
    if expected_class is not None and cls != expected_class:
        raise ValueError(
            f"gateway_name={gateway_name!r} 分类为 {cls!r}，"
            f"与期望 {expected_class!r} 不符（避免类与命名错配）。"
        )


def validate_node_mode(mode: str) -> None:
    """节点 yaml 的 mode 字段校验。

    >>> validate_node_mode("live")
    >>> validate_node_mode("sim")
    >>> try:
    ...     validate_node_mode("prod")
    ... except ValueError as e:
    ...     print("rejected")
    rejected
    """
    if mode not in VALID_NODE_MODES:
        raise ValueError(
            f"node.mode={mode!r} 违反约定，必须是 {VALID_NODE_MODES} 之一。"
            f"详见 vnpy_common/naming.py 模块 docstring。"
        )
