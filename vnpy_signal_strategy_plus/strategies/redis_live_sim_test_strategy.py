# -*- coding: utf-8 -*-
"""聚宽 Redis 信号 -> MySQL -> vnpy -> 模拟柜台的近实盘链路测试策略。

这个策略刻意不读取 ``position.csv``，也不做 CSV 持仓引导。它用于验证
聚宽回测实时写入 Redis 后，本地 bridge、MySQL 信号表、vnpy 策略下单、
QMT_SIM 撮合和 WebTrader/mlearnweb 展示这条链路。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from vnpy_signal_strategy_plus.strategies.csv_replay_test_strategy import (
    CsvReplayTestStrategy,
    DEFAULT_CALENDAR_PROVIDER_URI,
    _expand_path_setting,
    _resolve_setting_path,
)


REDIS_LIVE_SIM_SETTING_PATH = _resolve_setting_path(
    Path(__file__).resolve().parent.parent / "test" / "redis_live_sim_setting.json"
)


class RedisLiveSimTestStrategy(CsvReplayTestStrategy):
    """Redis 信号近实盘模拟链路策略。

    配置语义：
    - ``replay.mode = "sim_replay"``：按 ``stock_trade.remark`` 的历史时间推进
      QMT_SIM，适合聚宽回测写历史信号后的本地模拟成交。
    - ``replay.mode = "live"``：退回父类实时轮询，只消费当前交易日信号，便于
      后续切换真实网关时复用策略入口。
    """

    author = "redis-live-sim"
    strategy_name = "etf_rotation_basic"

    def load_external_setting(self) -> None:
        """加载近实盘链路配置，并显式禁用 CSV 持仓引导。"""
        if not REDIS_LIVE_SIM_SETTING_PATH.exists():
            self.write_log(
                f"[redis-live-sim] 未找到测试配置: {REDIS_LIVE_SIM_SETTING_PATH}"
            )
            return

        try:
            with open(REDIS_LIVE_SIM_SETTING_PATH, "r", encoding="utf-8-sig") as f:
                setting = json.load(f)
        except Exception as exc:
            self.write_log(f"[redis-live-sim] 配置读取失败: {exc}")
            return

        try:
            mysql_cfg = setting["mysql"]
            self.db_host = mysql_cfg["host"]
            self.db_port = int(mysql_cfg["port"])
            self.db_user = mysql_cfg["user"]
            self.db_password = mysql_cfg["password"]
            self.db_name = mysql_cfg["db"]
        except KeyError as exc:
            self.write_log(f"[redis-live-sim] 配置缺少 mysql 字段: {exc}")
            return

        strategy_cfg = setting.get("strategy", {}) or {}
        self.poll_interval = float(strategy_cfg.get("poll_interval", 0.05))
        self.engine_type = "实盘"
        self.start_date = str(strategy_cfg.get("start_date", "20190101 00:00:00"))
        self.end_date = str(strategy_cfg.get("end_date", "20300101 00:00:00"))

        gateway_cfg = setting.get("gateway", {}) or setting.get("sim", {}) or {}
        self.gateway = (
            gateway_cfg.get("gateway_name")
            or gateway_cfg.get("account_id")
            or "QMT_SIM"
        )

        replay_cfg = setting.get("replay", {}) or {}
        replay_mode = str(replay_cfg.get("mode", "sim_replay"))
        self._replay_enabled = replay_mode == "sim_replay" and bool(
            replay_cfg.get("consume_historical_remarks", True)
        )
        self._idle_settle_seconds = float(replay_cfg.get("idle_settle_seconds", 30))
        self._calendar_provider_uri = str(
            _expand_path_setting(
                replay_cfg.get("calendar_provider_uri")
                or setting.get("calendar_provider_uri")
                or DEFAULT_CALENDAR_PROVIDER_URI
            )
        )
        self._trade_calendar = None
        self._calendar_warning_logged = False

        # 近实盘链路从纯现金开始，严禁复用 CSV 持仓引导字段。
        self._date_range_start: Optional[str] = None
        self._csv_position_path: Optional[Path] = None
        self._csv_encoding = "gbk"

        if (setting.get("csv") or {}).get("position_path"):
            self.write_log("[redis-live-sim] 已忽略 csv.position_path；本链路不做持仓引导")
        if replay_cfg.get("date_range"):
            self.write_log("[redis-live-sim] 已忽略 replay.date_range；本链路消费聚宽写入的信号")

        password_mask = "***" if self.db_password else ""
        self.write_log(
            f"[redis-live-sim] 配置生效 host={self.db_host}:{self.db_port} "
            f"db={self.db_name} user={self.db_user} pwd={password_mask} "
            f"gateway={self.gateway} replay_mode={replay_mode} "
            f"replay_enabled={self._replay_enabled} csv_seed=disabled "
            f"idle_settle={self._idle_settle_seconds}s"
        )
