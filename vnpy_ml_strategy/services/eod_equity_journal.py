"""Persist live/sim-live daily equity facts into replay history.

``MLEngine`` owns vnpy app lifecycle wiring. This service owns the concrete
daily equity journaling rules so the engine does not grow order/account
persistence details.
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time
from typing import Any, Callable, Mapping, Optional, Tuple

from loguru import logger

from ..replay_history import write_snapshot


class EodEquityJournalService:
    """Journal one EOD equity snapshot per active strategy and trade day."""

    def __init__(
        self,
        *,
        main_engine: Any,
        strategies: Mapping[str, Any],
        is_trade_day: Callable[[date], bool],
        now_provider: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.main_engine = main_engine
        self.strategies = strategies
        self.is_trade_day = is_trade_day
        self.now_provider = now_provider
        self._persisted_keys: set[str] = set()

    def on_timer(self) -> None:
        """Run all EOD journal producers from the vnpy timer event."""
        try:
            self.persist_sim_live_eod_equity_from_settled_gateways()
            self.persist_broker_live_eod_equity_after_close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EodEquityJournal] timer failed: {}", exc)

    def _running_strategies_for_gateway(self, gateway_name: str) -> list[Any]:
        """Return active strategies bound to a gateway."""
        out: list[Any] = []
        for strat in self.strategies.values():
            if getattr(strat, "gateway", "") != gateway_name:
                continue
            if not getattr(strat, "inited", False):
                continue
            if not getattr(strat, "trading", False):
                continue
            out.append(strat)
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
        """Return account-level total equity for non-sim gateways."""
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

    def _write_eod_equity_snapshot(
        self,
        *,
        strat: Any,
        settle_day: date,
        source: str,
        equity: float,
        positions_count: int,
    ) -> bool:
        """Write one strategy EOD equity row into vnpy replay_history.db."""
        raw_variables: dict[str, Any] = {}
        get_variables = getattr(strat, "get_variables", None)
        if callable(get_variables):
            try:
                raw_variables.update(get_variables() or {})
            except Exception:
                pass
        raw_variables.update({
            "journal_source": source,
            "gateway": getattr(strat, "gateway", ""),
            "settle_date": settle_day.isoformat(),
        })

        ok = write_snapshot(
            strategy_name=getattr(strat, "strategy_name", ""),
            ts=datetime.combine(settle_day, dt_time(hour=15)),
            strategy_value=equity,
            account_equity=equity,
            positions_count=positions_count,
            raw_variables=raw_variables,
        )
        if ok:
            logger.info(
                "[EodEquityJournal][{}] persisted source={} day={} equity={:.2f}",
                getattr(strat, "strategy_name", ""),
                source,
                settle_day,
                equity,
            )
        return ok

    def persist_sim_live_eod_equity_from_settled_gateways(self) -> None:
        """Persist sim-live EOD equity after QmtSimGateway has settled."""
        gateway_names = {
            getattr(strat, "gateway", "")
            for strat in self.strategies.values()
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
            for strat in self._running_strategies_for_gateway(gateway_name):
                key = (
                    f"sim_live_settle:{gateway_name}:"
                    f"{getattr(strat, 'strategy_name', '')}:{settle_day.isoformat()}"
                )
                if key in self._persisted_keys:
                    continue
                if self._write_eod_equity_snapshot(
                    strat=strat,
                    settle_day=settle_day,
                    source="sim_live_settle",
                    equity=equity,
                    positions_count=positions_count,
                ):
                    self._persisted_keys.add(key)

    def persist_broker_live_eod_equity_after_close(self) -> None:
        """Persist broker live EOD equity after A-share close.

        Real broker gateways do not expose ``counter.last_settle_date``. For
        them, write one account-level total-asset point after 15:10 on trading
        days. This matches the existing account_equity fallback semantics when
        multiple strategies share the same broker account.
        """
        now = self.now_provider()
        if now.time() < dt_time(hour=15, minute=10):
            return
        settle_day = now.date()
        if not self.is_trade_day(settle_day):
            return

        gateway_names = {
            getattr(strat, "gateway", "")
            for strat in self.strategies.values()
            if getattr(strat, "gateway", "")
        }
        for gateway_name in gateway_names:
            try:
                gateway = self.main_engine.get_gateway(gateway_name)
            except Exception:
                continue
            if getattr(getattr(gateway, "td", None), "counter", None) is not None:
                continue
            account_equity = self._gateway_account_equity(gateway_name)
            if account_equity is None:
                continue
            equity, positions_count = account_equity
            for strat in self._running_strategies_for_gateway(gateway_name):
                key = (
                    f"broker_live_close:{gateway_name}:"
                    f"{getattr(strat, 'strategy_name', '')}:{settle_day.isoformat()}"
                )
                if key in self._persisted_keys:
                    continue
                if self._write_eod_equity_snapshot(
                    strat=strat,
                    settle_day=settle_day,
                    source="broker_live_close",
                    equity=equity,
                    positions_count=positions_count,
                ):
                    self._persisted_keys.add(key)
