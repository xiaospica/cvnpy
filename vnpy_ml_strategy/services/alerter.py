"""[P1-3 方案 A] vnpy 主进程关键事件邮件告警.

把"机器在跑但业务出问题"的事件通过 vnpy 自带 EmailEngine 发邮件给运维.
依赖 vt_setting.json 的 ``email.*`` 字段 (server / port / username / password /
sender / receiver). 这些字段为空时 EmailEngine.send_email 会 silent 失败 —
init 时主动检查并 log warn.

监听的事件:
  EVENT_DAILY_INGEST_FAILED  — 20:00 拉数据 / filter / qlib bin dump 失败
                               (TushareDatafeedPro 触发, payload 含 stage / error)
  EVENT_ML_METRICS<name>     — 推理 publish_metrics 后, 仅 status=failed 时发警告
                               (status=ok / empty 不发, 否则刷屏)

去重逻辑 (避免同 1 个故障被 cron 反复触发刷邮箱):
  按 (event_kind, identifier) 维度去重, 同一 key 60 min 内只发 1 封.
  identifier:
    - ingest_failed: trade_date
    - ml_metrics_failed: strategy_name + trade_date

⚠️ TODO 升级路径 — 长期接 SaaS 监控:
  本方案是"事件触发邮件", 缺点:
    1. mlearnweb / vnpy 主进程整个挂了 → alerter 也挂 → 收不到任何告警
    2. 邮件易被忽略, 紧急程度低
    3. 没有 Dashboard 看历史告警 / 趋势
  长期升级: 接 Uptime Kuma (自托管, 完全免费) 或 Healthchecks.io (海外 SaaS).
  详见 docs/deployment_windows.md §P1-3 SaaS 方案对比.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from vnpy.event import Event
from vnpy.trader.setting import SETTINGS

logger = logging.getLogger(__name__)


# 同一 key 邮件冷却时间 (秒): 60 min 内同一故障只发 1 封, 防止刷屏
DEDUP_COOLDOWN_SECONDS = 60 * 60


class Alerter:
    """关键事件邮件告警器.

    用法:
        # MLEngine.init_engine 里:
        from .services.alerter import Alerter
        self.alerter = Alerter(self.main_engine)
        self.alerter.register_listeners(self.event_engine)
    """

    def __init__(self, main_engine):
        self.main_engine = main_engine
        # {(kind, identifier): last_sent_unix_ts}
        self._dedup: Dict[tuple, float] = {}

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def is_email_configured(self) -> bool:
        """vt_setting email.* 字段齐全才算配置好. 缺任一字段 → 不发邮件."""
        for k in ("email.server", "email.username", "email.password",
                  "email.sender", "email.receiver"):
            if not SETTINGS.get(k):
                return False
        return True

    def register_listeners(self, event_engine) -> None:
        """注册事件监听. main_engine 启动期 init_engine 调一次即可."""
        if not self.is_email_configured():
            logger.warning(
                "[alerter] vt_setting email.* 未配置完整, 告警邮件不会发送. "
                "实盘上线前请填 .vntrader/vt_setting.json 的 email.server/port/"
                "username/password/sender/receiver 字段."
            )
            # 仍然注册监听 (即使邮件不发, 也走 log 记录)

        # 监听 daily_ingest 失败
        try:
            from vnpy_tushare_pro.engine import EVENT_DAILY_INGEST_FAILED
            event_engine.register(EVENT_DAILY_INGEST_FAILED, self._on_ingest_failed)
            logger.info("[alerter] 已注册 EVENT_DAILY_INGEST_FAILED 监听")
        except ImportError:
            logger.info("[alerter] vnpy_tushare_pro 未加载, 跳过 ingest 告警")

        # 监听 ml metrics (失败状态时告警)
        # EVENT_ML_METRICS = 'eMlMetrics.' + strategy_name (per-策略 topic)
        # EventEngine 只有 register(type, handler), 不支持前缀匹配.
        # 改成在 publish_metrics 时统一发 EVENT_ML_METRICS_ALERT (不带 strategy_name)
        # 让 alerter 监听单一 topic. 简化: 监听通用 'eMlMetricsAlert' (本仓库新加).
        from vnpy_ml_strategy.base import EVENT_ML_METRICS_ALERT
        event_engine.register(EVENT_ML_METRICS_ALERT, self._on_ml_metrics_alert)
        logger.info("[alerter] 已注册 EVENT_ML_METRICS_ALERT 监听")

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_ingest_failed(self, event: Event) -> None:
        payload = event.data or {}
        trade_date = payload.get("trade_date", "unknown")
        stage = payload.get("stage", "unknown")
        error = payload.get("error", "")
        duration_s = payload.get("duration_s", 0)

        subject = f"[vnpy 告警] daily_ingest 失败 trade_date={trade_date}"
        content = (
            f"trade_date: {trade_date}\n"
            f"stage:      {stage}\n"
            f"duration_s: {duration_s:.1f}\n"
            f"error:      {error}\n\n"
            f"影响: 21:00 推理可能找不到 filter snapshot 直接 raise; "
            f"建议: 检查 tushare API 连通性 / 磁盘空间 / 聚宽 CSV 是否陈旧.\n"
            f"详见日志: D:/vnpy_logs/vnpy_headless.log (NSSM 部署) 或控制台输出.\n"
        )
        self._send_dedup(kind="ingest_failed", identifier=str(trade_date),
                         subject=subject, content=content)

    def _on_ml_metrics_alert(self, event: Event) -> None:
        """publish_metrics 时如 status='failed' 由 engine.publish_metrics 主动发此事件.

        payload: {strategy, trade_date, status, error_message, ...}
        """
        payload = event.data or {}
        strategy = payload.get("strategy", "unknown")
        trade_date = payload.get("trade_date", "")
        status = payload.get("status", "")
        error = payload.get("error_message") or payload.get("error", "")

        # 仅 failed 状态发邮件 (ok / empty 不发)
        if status != "failed":
            return

        subject = f"[vnpy 告警] 推理失败 strategy={strategy} trade_date={trade_date}"
        content = (
            f"strategy:   {strategy}\n"
            f"trade_date: {trade_date}\n"
            f"status:     {status}\n"
            f"error:      {error}\n\n"
            f"影响: 当日无信号产出, 09:26 rebalance 不下单; "
            f"建议: 检查 D:/ml_output/{strategy}/{trade_date}/diagnostics.json 详细错误; "
            f"qlib_data_bin 当日数据是否齐 (calendar 末尾 == {trade_date}); "
            f"filter snapshot 是否就位.\n"
        )
        self._send_dedup(kind="ml_metrics_failed",
                         identifier=f"{strategy}:{trade_date}",
                         subject=subject, content=content)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _send_dedup(self, *, kind: str, identifier: str, subject: str, content: str) -> None:
        """按 (kind, identifier) 去重. 60 min 内同 key 只发 1 封."""
        now = time.time()
        key = (kind, identifier)
        last = self._dedup.get(key)
        if last is not None and now - last < DEDUP_COOLDOWN_SECONDS:
            logger.info(
                f"[alerter] {kind}:{identifier} 距上次告警 {now-last:.0f}s "
                f"< {DEDUP_COOLDOWN_SECONDS}s, 跳过 (去重)"
            )
            return

        if not self.is_email_configured():
            logger.warning(f"[alerter] 跳过发邮件 (email 未配置): {subject}")
            self._dedup[key] = now  # 即便没发也算, 避免每次都 log
            return

        try:
            self.main_engine.send_email(subject, content)
            self._dedup[key] = now
            logger.info(f"[alerter] sent: {subject}")
        except Exception as exc:
            logger.warning(f"[alerter] send_email 失败: {exc}")
