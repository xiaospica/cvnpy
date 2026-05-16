"""Common live/sim-live EOD equity journal service."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Mapping, Optional, Tuple

from loguru import logger

from vnpy_common.persistence.strategy_equity_journal import (
    SOURCE_BROKER_LIVE_CLOSE,
    SOURCE_SIM_LIVE_SETTLE,
    write_snapshot,
)
from vnpy_common.persistence.strategy_trade_journal import list_strategy_trades


BROKER_LIVE_EOD_JOURNAL_TIME_ENV = "VNPY_BROKER_LIVE_EOD_JOURNAL_TIME"
DEFAULT_BROKER_LIVE_EOD_JOURNAL_TIME = dt_time(hour=16)
STRATEGY_INITIAL_CAPITALS_ENV = "VNPY_STRATEGY_INITIAL_CAPITALS"
STRATEGY_DEFAULT_INITIAL_CAPITAL_ENV = "VNPY_DEFAULT_STRATEGY_INITIAL_CAPITAL"


@dataclass(frozen=True)
class StrategyProvider:
    engine: str
    strategies: Mapping[str, Any]


class StrategyEquityJournalService:
    """Journal one EOD equity snapshot per active strategy and trade day."""

    def __init__(
        self,
        *,
        main_engine: Any,
        is_trade_day: Optional[Callable[[date], bool]] = None,
        now_provider: Callable[[], datetime] = datetime.now,
        broker_live_eod_time: Optional[dt_time] = None,
    ) -> None:
        self.main_engine = main_engine
        self.is_trade_day = is_trade_day or self._weekday_trade_day
        self.now_provider = now_provider
        self.broker_live_eod_time = (
            broker_live_eod_time or self._load_broker_live_eod_time()
        )
        self.strategy_initial_capitals = self._load_strategy_initial_capitals()
        self._providers: list[StrategyProvider] = []
        self._persisted_keys: set[str] = set()

    @staticmethod
    def _weekday_trade_day(day: date) -> bool:
        return day.weekday() < 5

    @staticmethod
    def _parse_time(value: str) -> Optional[dt_time]:
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
        return None

    @classmethod
    def _load_broker_live_eod_time(cls) -> dt_time:
        value = os.getenv(BROKER_LIVE_EOD_JOURNAL_TIME_ENV, "").strip()
        parsed = cls._parse_time(value)
        if parsed is not None:
            return parsed
        if value:
            logger.warning(
                "[StrategyEquityJournal] invalid {}={!r}, fallback to {}",
                BROKER_LIVE_EOD_JOURNAL_TIME_ENV,
                value,
                DEFAULT_BROKER_LIVE_EOD_JOURNAL_TIME.strftime("%H:%M"),
            )
        return DEFAULT_BROKER_LIVE_EOD_JOURNAL_TIME

    @staticmethod
    def _load_strategy_initial_capitals() -> dict[str, float]:
        raw = os.getenv(STRATEGY_INITIAL_CAPITALS_ENV, "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[StrategyEquityJournal] invalid {} JSON: {}",
                STRATEGY_INITIAL_CAPITALS_ENV,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "[StrategyEquityJournal] {} must be a JSON object",
                STRATEGY_INITIAL_CAPITALS_ENV,
            )
            return {}
        out: dict[str, float] = {}
        for key, value in data.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                logger.warning(
                    "[StrategyEquityJournal] skip invalid capital {}={!r}",
                    key,
                    value,
                )
        return out

    def register_provider(self, *, engine: str, strategies: Mapping[str, Any]) -> None:
        self._providers.append(StrategyProvider(engine=str(engine), strategies=strategies))

    def on_timer(self) -> None:
        """Run all EOD journal producers from the vnpy timer event."""
        try:
            self.persist_sim_live_eod_equity_from_settled_gateways()
            self.persist_broker_live_eod_equity_after_close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[StrategyEquityJournal] timer failed: {}", exc)

    def _iter_strategies(self):
        for provider in self._providers:
            for strategy in provider.strategies.values():
                yield provider.engine, strategy

    def _running_strategies_for_gateway(self, gateway_name: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for engine, strat in self._iter_strategies():
            if getattr(strat, "gateway", "") != gateway_name:
                continue
            if not getattr(strat, "inited", False):
                continue
            if not getattr(strat, "trading", False):
                continue
            if str(getattr(strat, "replay_status", "") or "") == "running":
                continue
            out.append((engine, strat))
        return out

    @staticmethod
    def _normalize_settle_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value[:10]).date()
            except ValueError:
                return None
        return None

    @staticmethod
    def _counter_equity(counter: Any) -> Tuple[float, int]:
        cash = float(getattr(counter, "capital", 0.0) or 0.0) - float(
            getattr(counter, "frozen", 0.0) or 0.0
        )
        market_value = 0.0
        positions_count = 0
        for pos in (getattr(counter, "positions", {}) or {}).values():
            volume = float(getattr(pos, "volume", 0.0) or 0.0)
            if volume <= 0:
                continue
            price = float(getattr(pos, "price", 0.0) or 0.0)
            pnl = float(getattr(pos, "pnl", 0.0) or 0.0)
            market_value += volume * price + pnl
            positions_count += 1
        return cash + market_value, positions_count

    def _gateway_account_equity(self, gateway_name: str) -> Optional[Tuple[float, int]]:
        accounts = [
            acc for acc in self.main_engine.get_all_accounts()
            if getattr(acc, "gateway_name", "") == gateway_name
        ]
        if not accounts:
            return None
        positions_count = sum(
            1 for pos in self.main_engine.get_all_positions()
            if getattr(pos, "gateway_name", "") == gateway_name
            and float(getattr(pos, "volume", 0.0) or 0.0) > 0
        )
        return float(getattr(accounts[0], "balance", 0.0) or 0.0), positions_count

    def _strategy_initial_capital(
        self,
        *,
        engine: str,
        gateway_name: str,
        strat: Any,
    ) -> Optional[float]:
        strategy_name = str(getattr(strat, "strategy_name", "") or "")
        keys = (
            f"{gateway_name}:{engine}:{strategy_name}",
            f"{engine}:{strategy_name}",
            strategy_name,
        )
        for key in keys:
            if key in self.strategy_initial_capitals:
                return self.strategy_initial_capitals[key]

        get_parameters = getattr(strat, "get_parameters", None)
        params: dict[str, Any] = {}
        if callable(get_parameters):
            try:
                params = get_parameters() or {}
            except Exception:
                params = {}

        for name in (
            "initial_capital",
            "allocated_capital",
            "strategy_capital",
            "capital",
        ):
            value = params.get(name, getattr(strat, name, None))
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

        default_raw = os.getenv(STRATEGY_DEFAULT_INITIAL_CAPITAL_ENV, "").strip()
        if default_raw:
            try:
                return float(default_raw)
            except ValueError:
                logger.warning(
                    "[StrategyEquityJournal] invalid {}={!r}",
                    STRATEGY_DEFAULT_INITIAL_CAPITAL_ENV,
                    default_raw,
                )
        return None

    def _unit_market_values(self, gateway_name: str) -> dict[str, float]:
        values: dict[str, float] = {}
        try:
            positions = self.main_engine.get_all_positions()
        except Exception:
            return values
        for pos in positions:
            if getattr(pos, "gateway_name", "") != gateway_name:
                continue
            volume = float(getattr(pos, "volume", 0.0) or 0.0)
            if volume <= 0:
                continue
            vt_symbol = str(getattr(pos, "vt_symbol", "") or "")
            price = float(getattr(pos, "price", 0.0) or 0.0)
            pnl = float(getattr(pos, "pnl", 0.0) or 0.0)
            market_value = price * volume + pnl
            if market_value <= 0:
                market_value = price * volume
            if vt_symbol:
                values[vt_symbol] = market_value / volume
        return values

    def _attributed_broker_live_equity(
        self,
        *,
        engine: str,
        gateway_name: str,
        strat: Any,
    ) -> Optional[tuple[float, int, dict[str, Any]]]:
        """Build strategy equity from attributed broker trades.

        This path is precise at the strategy ownership level when orders carry
        ``{strategy_name}:{seq}`` references and a per-strategy initial capital
        is configured. Fees are not available from current ``TradeData`` and are
        explicitly marked in ``raw_variables``.
        """
        strategy_name = str(getattr(strat, "strategy_name", "") or "")
        if not strategy_name:
            return None
        initial_capital = self._strategy_initial_capital(
            engine=engine,
            gateway_name=gateway_name,
            strat=strat,
        )
        if initial_capital is None:
            return None

        trades = list_strategy_trades(
            gateway_name=gateway_name,
            strategy_name=strategy_name,
        )
        if not trades:
            return None

        cash = float(initial_capital)
        positions: dict[str, float] = {}
        for trade in trades:
            vt_symbol = str(trade.get("vt_symbol") or "")
            direction = str(trade.get("direction") or "")
            price = float(trade.get("price") or 0.0)
            volume = float(trade.get("volume") or 0.0)
            if not vt_symbol or price <= 0 or volume <= 0:
                continue
            amount = price * volume
            if direction in {"多", "LONG", "long"}:
                cash -= amount
                positions[vt_symbol] = positions.get(vt_symbol, 0.0) + volume
            elif direction in {"空", "SHORT", "short"}:
                cash += amount
                positions[vt_symbol] = positions.get(vt_symbol, 0.0) - volume

        unit_values = self._unit_market_values(gateway_name)
        market_value = 0.0
        positions_count = 0
        missing_prices: list[str] = []
        for vt_symbol, volume in positions.items():
            if volume <= 0:
                continue
            unit_value = unit_values.get(vt_symbol)
            if unit_value is None:
                missing_prices.append(vt_symbol)
                continue
            market_value += volume * unit_value
            positions_count += 1

        raw = {
            "attribution_method": "strategy_trade_journal",
            "initial_capital": initial_capital,
            "cash": cash,
            "market_value": market_value,
            "trades_count": len(trades),
            "open_symbols": sorted(k for k, v in positions.items() if v > 0),
            "missing_market_price_symbols": missing_prices,
            "fee_note": "TradeData currently has no commission/tax fields; cash attribution excludes fees.",
        }
        return cash + market_value, positions_count, raw

    def _write_eod_equity_snapshot(
        self,
        *,
        engine: str,
        strat: Any,
        settle_day: date,
        source_label: str,
        equity: float,
        positions_count: int,
        account_equity: Optional[float] = None,
        extra_raw_variables: Optional[dict[str, Any]] = None,
    ) -> bool:
        raw_variables: dict[str, Any] = {}
        get_variables = getattr(strat, "get_variables", None)
        if callable(get_variables):
            try:
                raw_variables.update(get_variables() or {})
            except Exception:
                pass
        raw_variables.update({
            "journal_source": source_label,
            "gateway": getattr(strat, "gateway", ""),
            "settle_date": settle_day.isoformat(),
        })
        if extra_raw_variables:
            raw_variables.update(extra_raw_variables)

        ok = write_snapshot(
            engine=engine,
            strategy_name=getattr(strat, "strategy_name", ""),
            source_label=source_label,
            ts=datetime.combine(settle_day, dt_time(hour=15)),
            strategy_value=equity,
            account_equity=equity if account_equity is None else account_equity,
            positions_count=positions_count,
            raw_variables=raw_variables,
        )
        if ok:
            logger.info(
                "[StrategyEquityJournal][{}/{}] persisted source={} day={} equity={:.2f}",
                engine,
                getattr(strat, "strategy_name", ""),
                source_label,
                settle_day,
                equity,
            )
        return ok

    def persist_sim_live_eod_equity_from_settled_gateways(self) -> None:
        """Persist sim-live EOD equity after QmtSimGateway has settled."""
        gateway_names = {
            getattr(strat, "gateway", "")
            for _, strat in self._iter_strategies()
            if getattr(strat, "gateway", "")
        }
        for gateway_name in gateway_names:
            try:
                gateway = self.main_engine.get_gateway(gateway_name)
            except Exception:
                continue
            counter = getattr(getattr(gateway, "td", None), "counter", None)
            if counter is None:
                continue
            settle_day = self._normalize_settle_date(
                getattr(counter, "last_settle_date", None)
            )
            if settle_day is None:
                continue
            equity, positions_count = self._counter_equity(counter)
            for engine, strat in self._running_strategies_for_gateway(gateway_name):
                key = (
                    f"{SOURCE_SIM_LIVE_SETTLE}:{engine}:{gateway_name}:"
                    f"{getattr(strat, 'strategy_name', '')}:{settle_day.isoformat()}"
                )
                if key in self._persisted_keys:
                    continue
                if self._write_eod_equity_snapshot(
                    engine=engine,
                    strat=strat,
                    settle_day=settle_day,
                    source_label=SOURCE_SIM_LIVE_SETTLE,
                    equity=equity,
                    positions_count=positions_count,
                ):
                    self._persisted_keys.add(key)

    def persist_broker_live_eod_equity_after_close(self) -> None:
        """Persist broker live EOD equity after A-share close."""
        now = self.now_provider()
        if now.time() < self.broker_live_eod_time:
            return
        settle_day = now.date()
        if not self.is_trade_day(settle_day):
            return

        gateway_names = {
            getattr(strat, "gateway", "")
            for _, strat in self._iter_strategies()
            if getattr(strat, "gateway", "")
        }
        for gateway_name in gateway_names:
            try:
                gateway = self.main_engine.get_gateway(gateway_name)
            except Exception:
                continue
            if getattr(getattr(gateway, "td", None), "counter", None) is not None:
                continue
            gateway_account = self._gateway_account_equity(gateway_name)
            if gateway_account is None:
                continue
            gateway_account_equity, gateway_positions_count = gateway_account
            for engine, strat in self._running_strategies_for_gateway(gateway_name):
                attributed = self._attributed_broker_live_equity(
                    engine=engine,
                    gateway_name=gateway_name,
                    strat=strat,
                )
                if attributed is not None:
                    equity, positions_count, extra_raw = attributed
                    extra_raw["gateway_account_equity"] = gateway_account_equity
                else:
                    equity = gateway_account_equity
                    positions_count = gateway_positions_count
                    extra_raw = {
                        "attribution_method": "account_equity_fallback",
                        "attribution_reason": (
                            "missing strategy initial capital or attributed broker trades"
                        ),
                    }
                key = (
                    f"{SOURCE_BROKER_LIVE_CLOSE}:{engine}:{gateway_name}:"
                    f"{getattr(strat, 'strategy_name', '')}:{settle_day.isoformat()}"
                )
                if key in self._persisted_keys:
                    continue
                if self._write_eod_equity_snapshot(
                    engine=engine,
                    strat=strat,
                    settle_day=settle_day,
                    source_label=SOURCE_BROKER_LIVE_CLOSE,
                    equity=equity,
                    positions_count=positions_count,
                    account_equity=gateway_account_equity,
                    extra_raw_variables=extra_raw,
                ):
                    self._persisted_keys.add(key)
