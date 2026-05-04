"""[P1-3 Plan A] Alerter 单元测试.

覆盖:
  - is_email_configured: 字段齐全 / 缺失分支
  - _send_dedup: 60min 去重生效 / 不同 identifier 各自独立 / SMTP 未配置时不发但记入去重
  - _on_ingest_failed: subject/content 含 trade_date/stage/error
  - _on_ml_metrics_alert: status=failed 才发, ok/empty 跳过
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 让 import vnpy_ml_strategy.services.alerter 可用 - 因为 alerter 顶部
# from vnpy.event import Event / from vnpy.trader.setting import SETTINGS,
# 这些在测试机上一般已装. 否则 pytest collect 阶段会报错.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def alerter_module(monkeypatch):
    """Lazy import alerter, 拿到模块和 SETTINGS 引用便于 monkeypatch."""
    from vnpy_ml_strategy.services import alerter as mod
    return mod


@pytest.fixture
def main_engine():
    """Mock MainEngine, 提供 send_email."""
    me = MagicMock()
    me.send_email = MagicMock()
    return me


def _set_smtp(alerter_module, monkeypatch, **overrides):
    """SETTINGS 是 vnpy 的 dict 实例, 用 setitem monkeypatch 各 email.* 键."""
    defaults = {
        "email.server": "smtp.x.com",
        "email.username": "u",
        "email.password": "p",
        "email.sender": "s@x.com",
        "email.receiver": "r@x.com",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        monkeypatch.setitem(alerter_module.SETTINGS, k, v)


@pytest.fixture
def configured_alerter(alerter_module, main_engine, monkeypatch):
    """配置完整 SMTP 字段的 Alerter."""
    _set_smtp(alerter_module, monkeypatch)
    return alerter_module.Alerter(main_engine)


# ---------------------------------------------------------------------------
# is_email_configured
# ---------------------------------------------------------------------------


def test_is_email_configured_all_set(alerter_module, main_engine, monkeypatch):
    _set_smtp(alerter_module, monkeypatch)
    a = alerter_module.Alerter(main_engine)
    assert a.is_email_configured() is True


def test_is_email_configured_missing_field(alerter_module, main_engine, monkeypatch):
    """缺 email.password → False."""
    _set_smtp(alerter_module, monkeypatch, **{"email.password": ""})
    a = alerter_module.Alerter(main_engine)
    assert a.is_email_configured() is False


# ---------------------------------------------------------------------------
# _send_dedup 60min 去重
# ---------------------------------------------------------------------------


def test_send_dedup_within_cooldown_skipped(configured_alerter, main_engine):
    a = configured_alerter
    a._send_dedup(kind="x", identifier="2026-04-30", subject="s1", content="c1")
    a._send_dedup(kind="x", identifier="2026-04-30", subject="s2", content="c2")
    assert main_engine.send_email.call_count == 1, \
        "60min 内同 (kind, identifier) 应只发 1 次"


def test_send_dedup_after_cooldown_fires_again(
    configured_alerter, main_engine, alerter_module, monkeypatch,
):
    a = configured_alerter
    a._send_dedup(kind="x", identifier="d", subject="s1", content="c1")
    # 时间倒拨到刚好超过 cooldown
    fake_now = [time.time() + alerter_module.DEDUP_COOLDOWN_SECONDS + 1]
    monkeypatch.setattr(alerter_module.time, "time", lambda: fake_now[0])
    a._send_dedup(kind="x", identifier="d", subject="s2", content="c2")
    assert main_engine.send_email.call_count == 2


def test_send_dedup_different_identifier_independent(configured_alerter, main_engine):
    a = configured_alerter
    a._send_dedup(kind="x", identifier="a", subject="s", content="c")
    a._send_dedup(kind="x", identifier="b", subject="s", content="c")
    assert main_engine.send_email.call_count == 2


def test_send_dedup_smtp_unconfigured_marks_dedup(
    alerter_module, main_engine, monkeypatch,
):
    """SMTP 未配置时不发邮件, 但仍应记入 _dedup, 避免每次都 log."""
    _set_smtp(
        alerter_module, monkeypatch,
        **{"email.server": "", "email.username": "", "email.password": "",
           "email.sender": "", "email.receiver": ""},
    )
    a = alerter_module.Alerter(main_engine)
    a._send_dedup(kind="x", identifier="d", subject="s", content="c")
    assert main_engine.send_email.call_count == 0
    assert ("x", "d") in a._dedup


# ---------------------------------------------------------------------------
# 事件 handler
# ---------------------------------------------------------------------------


def test_on_ingest_failed_builds_email(configured_alerter, main_engine):
    a = configured_alerter
    event = MagicMock()
    event.data = {
        "trade_date": "20260429",
        "stage": "tushare_pull",
        "error": "API timeout",
        "duration_s": 12.3,
    }
    a._on_ingest_failed(event)
    assert main_engine.send_email.called
    subject, content = main_engine.send_email.call_args[0]
    assert "20260429" in subject
    assert "tushare_pull" in content
    assert "API timeout" in content


def test_on_ml_metrics_alert_only_failed(configured_alerter, main_engine):
    a = configured_alerter
    # status=ok 不发
    e1 = MagicMock(data={"strategy": "csi300", "trade_date": "20260429", "status": "ok"})
    a._on_ml_metrics_alert(e1)
    assert main_engine.send_email.call_count == 0

    # status=empty 不发
    e2 = MagicMock(data={"strategy": "csi300", "trade_date": "20260429", "status": "empty"})
    a._on_ml_metrics_alert(e2)
    assert main_engine.send_email.call_count == 0

    # status=failed 发
    e3 = MagicMock(data={
        "strategy": "csi300", "trade_date": "20260429",
        "status": "failed", "error_message": "model load oom",
    })
    a._on_ml_metrics_alert(e3)
    assert main_engine.send_email.call_count == 1
    subject, content = main_engine.send_email.call_args[0]
    assert "csi300" in subject
    assert "model load oom" in content


def test_on_ml_metrics_alert_dedup_per_strategy_date(configured_alerter, main_engine):
    """ml_metrics_failed identifier = strategy:trade_date, 同 key 不重复."""
    a = configured_alerter
    e = MagicMock(data={
        "strategy": "csi300", "trade_date": "20260429",
        "status": "failed", "error_message": "x",
    })
    a._on_ml_metrics_alert(e)
    a._on_ml_metrics_alert(e)
    assert main_engine.send_email.call_count == 1
    # 不同 strategy 各自独立
    e2 = MagicMock(data={
        "strategy": "zz500", "trade_date": "20260429",
        "status": "failed", "error_message": "x",
    })
    a._on_ml_metrics_alert(e2)
    assert main_engine.send_email.call_count == 2
