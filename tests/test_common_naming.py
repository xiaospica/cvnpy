"""vnpy_common/naming.py 命名约定 validator 单元测试。"""
from __future__ import annotations

import pytest

from vnpy_common.naming import (
    VALID_NODE_MODES,
    classify_gateway,
    validate_gateway_name,
    validate_node_mode,
)


# ---- classify_gateway ----

@pytest.mark.parametrize("name,expected", [
    ("QMT_SIM", "sim"),
    ("QMT_SIM_csi300", "sim"),
    ("QMT_SIM_zz500", "sim"),
    ("QMT_SIM_alldata", "sim"),
    ("QMT_SIM_a1b2c3", "sim"),
    ("QMT", "live"),
    # 不匹配 → unknown
    ("qmt_sim_lower", "unknown"),
    ("QMT_SIMULATOR", "unknown"),
    ("QMTPaper", "unknown"),
    ("XTP", "unknown"),
    ("", "unknown"),
])
def test_classify_gateway_known_patterns(name: str, expected: str) -> None:
    assert classify_gateway(name) == expected


def test_classify_gateway_handles_non_string() -> None:
    assert classify_gateway(None) == "unknown"  # type: ignore[arg-type]
    assert classify_gateway(123) == "unknown"  # type: ignore[arg-type]


# ---- validate_gateway_name ----

def test_validate_gateway_name_accepts_legal_names() -> None:
    validate_gateway_name("QMT_SIM_csi300")
    validate_gateway_name("QMT_SIM")
    validate_gateway_name("QMT")


def test_validate_gateway_name_rejects_illegal() -> None:
    for bad in ["BAD", "qmt_sim_lower", "QMT_SIMULATOR", ""]:
        with pytest.raises(ValueError, match="违反命名约定"):
            validate_gateway_name(bad)


def test_validate_gateway_name_expected_class_match() -> None:
    validate_gateway_name("QMT_SIM_csi300", expected_class="sim")
    validate_gateway_name("QMT", expected_class="live")


def test_validate_gateway_name_expected_class_mismatch() -> None:
    """QmtSimGateway 用 'QMT' 名（类与命名错配）应拒绝。"""
    with pytest.raises(ValueError, match="与期望"):
        validate_gateway_name("QMT", expected_class="sim")
    with pytest.raises(ValueError, match="与期望"):
        validate_gateway_name("QMT_SIM_csi300", expected_class="live")


# ---- validate_node_mode ----

def test_validate_node_mode_accepts_valid() -> None:
    for m in VALID_NODE_MODES:
        validate_node_mode(m)


@pytest.mark.parametrize("bad", ["prod", "test", "PROD", "Live", "", None])
def test_validate_node_mode_rejects_invalid(bad) -> None:
    with pytest.raises(ValueError, match="违反约定"):
        validate_node_mode(bad)
