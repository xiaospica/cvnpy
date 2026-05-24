# -*- coding: utf-8 -*-
"""MySQL v2 signal-journal polling strategy.

The legacy MySQL signal table is no longer consumed. Signals are read from
trade_signal_events and marked per strategy/account in strategy_signal_applications.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from threading import Thread

from sqlalchemy import and_, create_engine
from sqlalchemy.orm import sessionmaker

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import BarData, OrderData, TickData, TradeData

from .auto_resubmit import AutoResubmitMixinPlus
from .base import APP_NAME, CHINA_TZ, EngineType
from .signal_journal import (
    SignalJournalBase,
    StrategySignalApplication,
    TradeSignalEvent,
    record_signal_application,
)
from .template import SignalTemplatePlus
from .utils import choose_order_price, convert_code_to_vnpy_type


class MySQLSignalStrategyPlus(AutoResubmitMixinPlus, SignalTemplatePlus):
    """MySQL signal journal polling strategy."""

    author = "VeighNa"

    parameters = [
        "db_host", "db_port", "db_user",
        "db_password", "db_name", "poll_interval",
        "engine_type", "start_date", "end_date", "gateway",
        "pct_overflow_tolerance",
    ]
    variables = ["last_signal_id"]

    db_host = ""
    db_port = 3306
    db_user = "root"
    db_password = ""
    db_name = "mysql"
    poll_interval = 0.05
    engine_type = EngineType.LIVE
    start_date = "20250101 00:00:00"
    end_date = "20250201 00:00:00"
    gateway = ""
    live_orders_enabled = True
    live_signal_cutoff_dt = None
    pct_overflow_tolerance = 0.001
    board_lot_size = 100

    def __init__(self, signal_engine):
        super().__init__(signal_engine)

        self.last_signal_id = 0
        self.engine = None
        self.Session = None
        self.current_dt = None
        self.engine_type: EngineType | str = EngineType.LIVE
        self.signal_sim_thread = None

        self.is_polling_avtive = True
        self.poll_thread = None
        self._last_signal_orderids: list[str] = []

        self.load_external_setting()

    def load_external_setting(self):
        """Load setting from external json file."""
        config_path = Path(__file__).parent.joinpath("mysql_signal_setting.json")
        if not config_path.exists():
            self.write_log(f"未找到外部配置文件: {config_path}")
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                setting = json.load(f)

            if self.strategy_name in setting:
                self.update_setting(setting[self.strategy_name])
                self.write_log(f"成功加载策略配置: {self.strategy_name}")
            elif "default" in setting:
                self.update_setting(setting["default"])
                self.write_log("成功加载默认策略配置: default")
            else:
                self.write_log(f"配置文件中未找到策略配置: {self.strategy_name}")
                return

            password_mask = "***" if self.db_password else ""
            self.write_log(
                f"数据库配置生效 host={self.db_host}, port={self.db_port}, user={self.db_user}, "
                f"password={password_mask}, db={self.db_name}, poll_interval={self.poll_interval}"
            )
        except Exception as exc:
            self.write_log(f"加载外部配置失败: {exc}")

    def send_order(
        self,
        vt_symbol: str,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        order_type: OrderType = OrderType.LIMIT,
    ) -> list[str]:
        """Send an order, forcing A-share buy quantity to board lots."""
        if direction == Direction.LONG:
            old_volume = volume
            volume = (volume // 100) * 100
            if volume != old_volume:
                self.write_log(f"【数量修正】买入委托数量由 {old_volume} 调整为 {volume} (向下取整到100的整数倍)")

            if volume <= 0:
                self.write_log(f"【数量拦截】买入数量修正后不足100股，取消发单: {vt_symbol}")
                return []

        return super().send_order(vt_symbol, direction, offset, price, volume, order_type)

    def on_init(self):
        self.write_log("策略初始化")
        self.connect_db()

    def on_start(self):
        if isinstance(self.end_date, str):
            self.end_date = datetime.strptime(self.end_date, "%Y%m%d %H:%M:%S")
        if isinstance(self.start_date, str):
            self.start_date = datetime.strptime(self.start_date, "%Y%m%d %H:%M:%S")

        if self.engine_type == EngineType.LIVE.value:
            self.write_log("实盘策略启动")
            self.current_dt = datetime.now(CHINA_TZ)
        elif self.engine_type == EngineType.BACKTESTING.value:
            self.write_log("模拟策略启动")
            self.current_dt = self.start_date
            self.current_dt = datetime.combine(self.current_dt, datetime.max.time())
            self.signal_sim_thread = Thread(target=self.signal_simulation, daemon=True)
            self.signal_sim_thread.start()
        else:
            self.write_log(f"unsupported engine type: {self.engine_type}")

        self.restore_order_sequence_from_gateway()
        self.is_polling_avtive = True
        self.poll_thread = Thread(target=self.run_polling, daemon=True)
        self.poll_thread.start()

    def on_stop(self):
        self.write_log("策略停止")
        self.is_polling_avtive = False

    def on_tick(self, tick: TickData) -> None:
        pass

    def on_bar(self, bar: BarData) -> None:
        self.write_log(f"收到K线: {bar.datetime} {bar.close}")

    def on_order(self, order: OrderData) -> None:
        """Receive order callback and let auto-resubmit mixin handle it."""
        self.on_order_for_resubmit(order)

    def on_trade(self, trade: TradeData) -> None:
        pass

    def on_timer(self):
        """Drive auto-resubmit queue."""
        self.on_timer_for_resubmit()

    def signal_simulation(self):
        """Advance logical date for backtesting mode."""
        if self.engine_type == EngineType.BACKTESTING.value:
            while self.current_dt < self.end_date:
                if not self.is_polling_avtive:
                    break
                time.sleep(self.poll_interval + 0.5)
                self.current_dt += timedelta(days=1)
                self.write_log(f"模拟时间: {self.current_dt}")

    def _application_gateway_name(self) -> str:
        return str(self.gateway or "AUTO")

    def _signal_source_stg(self) -> str:
        """Return the shared source stg used to read trade signal events."""
        return str(getattr(self, "signal_source_stg", "") or self.strategy_name)

    def _scope_account_id(self, account_id: str) -> str:
        """Return a runner-scoped application account id when configured."""
        suffix = str(getattr(self, "signal_application_scope_suffix", "") or "").strip()
        if not suffix:
            return account_id

        scoped = f"{account_id}@{suffix}"
        if len(scoped) <= 64:
            return scoped

        digest = sha1(scoped.encode("utf-8")).hexdigest()[:12]
        return f"{scoped[:51]}:{digest}"

    def _application_account_id(self, gateway_name: str) -> str:
        try:
            for account in self.signal_engine.main_engine.get_all_accounts():
                if account.gateway_name == gateway_name:
                    return self._scope_account_id(str(account.accountid))
        except Exception:
            pass
        return self._scope_account_id(gateway_name)

    def _application_scope(self) -> tuple[str, str, str, str]:
        gateway_name = self._application_gateway_name()
        return (
            self._application_account_id(gateway_name),
            gateway_name,
            APP_NAME,
            str(self.strategy_name),
        )

    def query_trade_signals(self, session):
        """Query unconsumed v2 signal events for the current logical day."""
        if self.current_dt is None:
            return []

        today_start = datetime.combine(self.current_dt, datetime.min.time())
        signal_start = today_start
        cutoff_dt = getattr(self, "live_signal_cutoff_dt", None)
        if cutoff_dt is not None and self.engine_type in (EngineType.LIVE, EngineType.LIVE.value):
            if getattr(cutoff_dt, "tzinfo", None) is not None:
                cutoff_dt = cutoff_dt.astimezone(CHINA_TZ).replace(tzinfo=None)
            signal_start = max(today_start, cutoff_dt)
        account_id, gateway_name, engine, strategy_name = self._application_scope()
        app_join = and_(
            StrategySignalApplication.signal_event_id == TradeSignalEvent.id,
            StrategySignalApplication.account_id == account_id,
            StrategySignalApplication.gateway_name == gateway_name,
            StrategySignalApplication.engine == engine,
            StrategySignalApplication.strategy_name == strategy_name,
        )

        return (
            session.query(TradeSignalEvent)
            .outerjoin(StrategySignalApplication, app_join)
            .filter(
                TradeSignalEvent.stg == self._signal_source_stg(),
                TradeSignalEvent.remark >= signal_start,
                TradeSignalEvent.remark <= self.current_dt,
                StrategySignalApplication.id.is_(None),
            )
            .order_by(TradeSignalEvent.remark.asc(), TradeSignalEvent.id.asc())
            .limit(100)
            .all()
        )

    def mark_signal_consumed(
        self,
        session,
        signal: TradeSignalEvent,
        *,
        status: str,
        error_msg: str | None = None,
    ) -> None:
        """Persist the per-strategy consumption checkpoint for one signal."""
        account_id, gateway_name, engine, strategy_name = self._application_scope()
        record_signal_application(
            session,
            signal_event_id=int(signal.id),
            account_id=account_id,
            gateway_name=gateway_name,
            engine=engine,
            strategy_name=strategy_name,
            status=status,
            order_refs=list(self._last_signal_orderids),
            error_msg=error_msg,
        )

    def run_polling(self):
        """Poll v2 signal events in a background thread."""
        if (
            self.engine_type in (EngineType.LIVE, EngineType.LIVE.value)
            and not bool(getattr(self, "live_orders_enabled", True))
        ):
            self.write_log(
                "[live-safe] live_orders_enabled=False; skip MySQL signal polling. "
                "No signal will be consumed and no broker order can be sent."
            )
            while self.is_polling_avtive:
                time.sleep(max(float(self.poll_interval or 1.0), 1.0))
            return

        while self.is_polling_avtive:
            if not self.Session:
                time.sleep(self.poll_interval)
                continue

            if self.engine_type == EngineType.LIVE.value:
                self.current_dt = datetime.now(CHINA_TZ)

            if self.engine_type == EngineType.BACKTESTING.value:
                self.write_log(f"当前时间: {self.current_dt}")

            session = None
            try:
                session = self.Session()
                signals = self.query_trade_signals(session)

                for signal in signals:
                    if not self.is_polling_avtive:
                        break

                    self._last_signal_orderids = []
                    error_msg = None
                    try:
                        processed = self.process_signal(signal)
                    except Exception as exc:
                        processed = False
                        error_msg = f"{type(exc).__name__}: {exc}"
                        self.write_log(
                            f"处理信号失败: id={signal.id} uid={signal.signal_uid} {error_msg}\n"
                            f"{traceback.format_exc()}"
                        )

                    self.last_signal_id = max(self.last_signal_id, int(signal.id))
                    time.sleep(0.05)

                    try:
                        if processed:
                            status = "ordered" if self._last_signal_orderids else "skipped"
                            self.mark_signal_consumed(session, signal, status=status)
                            session.commit()
                        else:
                            session.rollback()
                    except Exception as exc:
                        session.rollback()
                        self.write_log(f"记录信号消费状态失败: id={signal.id} {exc}")

                session.close()
                session = None
                self.put_event()

            except Exception as exc:
                self.write_log(f"轮询出错: {exc}\n{traceback.format_exc()}")
                if session is not None:
                    try:
                        session.rollback()
                        session.close()
                    except Exception:
                        pass

            time.sleep(self.poll_interval)

    def process_signal(self, signal: TradeSignalEvent) -> bool:
        """Process one normalized v2 signal event."""
        self.write_log(
            f"收到信号: id={signal.id} uid={signal.signal_uid} "
            f"{signal.code} {signal.pct} {signal.signal_type} {signal.remark}"
        )

        vt_symbol = convert_code_to_vnpy_type(signal.code)

        direction = Direction.LONG
        offset = Offset.OPEN

        signal_type = str(signal.signal_type).lower()
        if "sell" in signal_type or "short" in signal_type:
            direction = Direction.SHORT
            offset = Offset.CLOSE
            if "open" in signal_type:
                offset = Offset.OPEN
        elif "buy" in signal_type or "long" in signal_type:
            direction = Direction.LONG
            offset = Offset.OPEN
            if "close" in signal_type:
                offset = Offset.CLOSE

        raw_pct = float(signal.pct)
        pct = self.normalize_signal_pct(raw_pct)
        if pct is None:
            self.write_log(f"百分比异常: {raw_pct}")
            return True
        if pct != raw_pct:
            self.write_log(
                f"百分比轻微超过 1，按满仓处理: raw_pct={raw_pct}, effective_pct={pct}"
            )
        fallback_price = float(signal.price or 0)
        empty_signal = self.is_empty_signal(signal)

        gateway_name = self.get_gateway_name(vt_symbol)
        if not gateway_name:
            self.write_log(f"无法获取网关名称，无法处理信号: {vt_symbol}")
            return True

        if pct <= 1.0:
            calc_price = self.get_best_price(vt_symbol, direction, fallback_price)
            if calc_price <= 0:
                self.write_log(f"无法获取{vt_symbol}有效价格，无法计算交易金额比例")
                return True

            total_capital = self.get_account_asset(gateway_name)
            if total_capital <= 0:
                self.write_log("账户权益为0，无法计算交易金额比例")
                return True

            target_value = total_capital * pct
            raw_volume = target_value / calc_price
            vol_int = self.round_to_board_lot(raw_volume)

            if vol_int <= 0 and direction == Direction.LONG:
                self.write_log(f"下单数量为0 (计算后: {vol_int})，忽略信号: {pct}")
                return True

            if direction == Direction.LONG:
                vol_int = self.cap_full_buy_volume_by_cash(
                    gateway_name=gateway_name,
                    price=calc_price,
                    requested_volume=vol_int,
                    pct=pct,
                )
                if vol_int <= 0:
                    self.write_log(
                        f"buy volume capped to 0 by available cash: {vt_symbol} pct={pct} price={calc_price}"
                    )
                    return True
                self.write_log(
                    f"交易金额比例计算(买入): 权益{total_capital} * 比例{pct} / 价格{calc_price} = 数量{vol_int}"
                )
            else:
                vt_positionid = f"{gateway_name}.{vt_symbol}.{Direction.LONG.value}"
                pos = None
                for position in self.signal_engine.main_engine.get_all_positions():
                    if position.vt_positionid == vt_positionid:
                        pos = position
                        break

                if not pos:
                    self.write_log(f"未找到持仓: {vt_positionid}")
                    return True

                available = int(pos.volume) - int(getattr(pos, "frozen", 0) or 0)
                if available <= 0:
                    self.write_log(
                        f"未找到可卖持仓(可用为0): {vt_positionid} volume={pos.volume} frozen={getattr(pos, 'frozen', 0)}"
                    )
                    return True

                vol_int = self.adjust_sell_volume_by_available_position(
                    vt_symbol=vt_symbol,
                    raw_volume=raw_volume,
                    rounded_volume=vol_int,
                    available=available,
                    empty_signal=empty_signal,
                )

                if vol_int <= 0:
                    self.write_log(
                        f"未找到可卖持仓(数量修正后为0): {vt_positionid} 可用={available}"
                    )
                    return True

                self.write_log(
                    f"交易金额比例计算(卖出): 权益{total_capital} * 比例{pct} / 价格{calc_price}; "
                    f"修正到可卖数量{vol_int}"
                )
        else:
            self.write_log(f"百分比异常: {pct}")
            return True

        price = self.get_order_price(vt_symbol, direction, fallback_price)
        order_type = OrderType.LIMIT
        if price <= 0:
            order_type = OrderType.MARKET
            price = 0

        vt_orderids = self.send_order(
            vt_symbol=vt_symbol,
            direction=direction,
            offset=offset,
            price=price,
            volume=vol_int,
            order_type=order_type,
        )
        self._last_signal_orderids = list(vt_orderids or [])

        if vt_orderids:
            self.write_log(f"下单成功: {vt_orderids}")
        else:
            self.write_log("下单失败")
        return True

    @staticmethod
    def is_empty_signal(signal: TradeSignalEvent) -> bool:
        """Return True when a signal explicitly asks to fully clear the position."""
        value = getattr(signal, "empty", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)

    def normalize_signal_pct(self, pct: float) -> float | None:
        """Clamp tiny over-100% JQ full-allocation drifts, reject real outliers."""
        if pct <= 1.0:
            return pct
        tolerance = float(getattr(self, "pct_overflow_tolerance", 0.001) or 0.0)
        if pct <= 1.0 + tolerance:
            return 1.0
        return None

    def round_to_board_lot(self, volume: float) -> int:
        """Round an A-share target quantity down to board lots."""
        lot_size = int(getattr(self, "board_lot_size", 100) or 100)
        if lot_size <= 0:
            lot_size = 100
        return int(volume // lot_size) * lot_size

    def is_near_full_buy_pct(self, pct: float) -> bool:
        """Return whether pct means a full or near-full buy intent."""
        tolerance = float(getattr(self, "pct_overflow_tolerance", 0.001) or 0.0)
        return pct >= max(0.0, 1.0 - tolerance)

    def get_gateway_counter(self, gateway_name: str):
        """Return a simulator-like counter when the gateway exposes one."""
        try:
            gateway = self.signal_engine.main_engine.get_gateway(gateway_name)
        except Exception:
            return None
        return getattr(getattr(gateway, "td", None), "counter", None)

    def get_account_available_cash(self, gateway_name: str) -> float:
        """Return synchronously available cash for pct-to-volume sizing."""
        counter = self.get_gateway_counter(gateway_name)
        if counter is not None and hasattr(counter, "capital"):
            try:
                return max(float(counter.capital) - float(getattr(counter, "frozen", 0.0) or 0.0), 0.0)
            except Exception:
                pass

        for account in self.signal_engine.main_engine.get_all_accounts():
            if account.gateway_name == gateway_name:
                available = getattr(account, "available", None)
                if available is None:
                    available = float(account.balance) - float(getattr(account, "frozen", 0.0) or 0.0)
                return max(float(available), 0.0)
        return 0.0

    def estimate_buy_frozen_cash(self, gateway_name: str, price: float, volume: int) -> float:
        """Estimate cash frozen by a buy order using gateway fees when possible."""
        if price <= 0 or volume <= 0:
            return 0.0
        amount = float(price) * int(volume)
        counter = self.get_gateway_counter(gateway_name)
        if counter is not None and hasattr(counter, "calculate_fee"):
            try:
                fee = float(counter.calculate_fee(amount, Direction.LONG))
                return amount + fee
            except Exception:
                pass

        # Keep the fallback aligned with QMT_SIM. Broker/gateway validation still
        # remains the final authority; this only avoids near-full pct sizing being
        # rejected by a few currency units.
        commission = max(amount * 0.0001, 5.0)
        transfer_fee = amount * 0.00001
        return amount + commission + transfer_fee

    def max_affordable_buy_volume(self, gateway_name: str, price: float, requested_volume: int) -> int:
        """Find the largest board-lot buy quantity affordable by available cash."""
        requested_volume = self.round_to_board_lot(requested_volume)
        if price <= 0 or requested_volume <= 0:
            return 0

        available_cash = self.get_account_available_cash(gateway_name)
        if available_cash <= 0:
            return 0

        lot_size = int(getattr(self, "board_lot_size", 100) or 100)
        high = requested_volume // lot_size
        low = 0
        while low < high:
            mid = (low + high + 1) // 2
            volume = mid * lot_size
            if self.estimate_buy_frozen_cash(gateway_name, price, volume) <= available_cash:
                low = mid
            else:
                high = mid - 1
        return low * lot_size

    def cap_full_buy_volume_by_cash(
        self,
        *,
        gateway_name: str,
        price: float,
        requested_volume: int,
        pct: float,
    ) -> int:
        """Cap near-full pct buys to the cash-affordable board-lot quantity."""
        requested_volume = self.round_to_board_lot(requested_volume)
        if not self.is_near_full_buy_pct(pct):
            return requested_volume

        capped_volume = self.max_affordable_buy_volume(gateway_name, price, requested_volume)
        if 0 < capped_volume < requested_volume:
            need_cash = self.estimate_buy_frozen_cash(gateway_name, price, requested_volume)
            capped_cash = self.estimate_buy_frozen_cash(gateway_name, price, capped_volume)
            available_cash = self.get_account_available_cash(gateway_name)
            self.write_log(
                "near-full buy volume capped by cash: "
                f"requested={requested_volume}, capped={capped_volume}, "
                f"available_cash={available_cash:.2f}, "
                f"requested_cash={need_cash:.2f}, capped_cash={capped_cash:.2f}"
            )
        return capped_volume

    def adjust_sell_volume_by_available_position(
        self,
        *,
        vt_symbol: str,
        raw_volume: float,
        rounded_volume: int,
        available: int,
        empty_signal: bool,
    ) -> int:
        """Apply sell-side pct sizing rules after reading current position."""
        if empty_signal:
            return available

        if (
            rounded_volume <= 0
            and 0 < raw_volume < self.board_lot_size
            and available >= self.board_lot_size
        ):
            self.write_log(
                f"tiny sell pct rounded to one board lot: {vt_symbol} "
                f"raw_volume={raw_volume:.4f} available={available}"
            )
            return self.board_lot_size

        if rounded_volume > available:
            return available
        return rounded_volume

    def get_gateway_name(self, vt_symbol: str) -> str | None:
        exchange_str = vt_symbol.split(".")[-1]
        gateway_name = ""
        if self.gateway:
            gateway_name = self.gateway
        else:
            contract = self.signal_engine.main_engine.get_contract(vt_symbol)
            if contract:
                gateway_name = contract.gateway_name
        if not gateway_name:
            for gw in self.signal_engine.main_engine.gateways.values():
                if Exchange(exchange_str) in gw.exchanges:
                    gateway_name = gw.gateway_name
                    break

        if not gateway_name:
            self.write_log(f"无法确定网关，无法查询持仓: {vt_symbol}")
            return None
        return gateway_name

    def get_active_tick(self, vt_symbol: str) -> TickData | None:
        gateway = None
        if self.gateway:
            gateway = self.signal_engine.main_engine.get_gateway(self.gateway)

        if gateway and hasattr(gateway, "get_full_tick"):
            try:
                tick = gateway.get_full_tick(vt_symbol)
                if tick:
                    return tick
            except Exception as exc:
                self.write_log(
                    f"主动获取五档行情异常: {vt_symbol} {exc}\n{traceback.format_exc()}"
                )

        return self.signal_engine.main_engine.get_tick(vt_symbol)

    def get_best_price(self, vt_symbol: str, direction: Direction, fallback_price: float) -> float:
        """Get sizing reference price."""
        tick = self.get_active_tick(vt_symbol)
        contract = self.signal_engine.main_engine.get_contract(vt_symbol)
        pricetick = contract.pricetick if contract else None
        return choose_order_price(tick, direction, fallback_price, pricetick)

    def get_order_price(self, vt_symbol: str, direction: Direction, fallback_price: float) -> float:
        """Get final order price."""
        return self.get_best_price(vt_symbol, direction, fallback_price)

    def connect_db(self):
        """Connect to MySQL and ensure v2 signal journal tables exist."""
        try:
            url = f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
            self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
            SignalJournalBase.metadata.create_all(self.engine)
            self.Session = sessionmaker(bind=self.engine)
            self.write_log("数据库连接成功，v2 信号表已确认")
        except Exception as exc:
            self.write_log(f"数据库连接失败: {exc}")

    def get_account_asset(self, gateway_name: str):
        for account in self.signal_engine.main_engine.get_all_accounts():
            if account.gateway_name == gateway_name:
                total_capital = float(account.balance)
                self.write_log(f"账户总资产(权益口径): {total_capital}")
                return total_capital
        self.write_log(f"未找到账户信息: {gateway_name}")
        return 0.0
