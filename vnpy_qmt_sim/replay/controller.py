"""Generic simulation replay controller."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

from .snapshot import calculate_gateway_equity, write_replay_snapshot


class ReplayStrategyAdapter(Protocol):
    """Protocol implemented by strategy-family replay adapters."""

    strategy_name: str
    gateway_name: str

    def prepare(self, days: Sequence[date]) -> None:
        """Prepare data needed by the replay window."""

    def before_day(self, day: date) -> None:
        """Hook before the day-open action."""

    def on_day_open(self, day: date) -> None:
        """Run the day-open strategy action."""

    def before_day_settle(self, day: date) -> None:
        """Hook after day-open action and before simulator settlement."""

    def on_day_close(self, day: date) -> None:
        """Hook after simulator settlement and snapshot persistence."""

    def after_replay(self, end_day: date) -> None:
        """Hook after the replay session finishes."""


TradeDayPredicate = Callable[[date], bool]
LogFunc = Callable[[str], None]


class SimReplayController:
    """Drive a ``QmtSimGateway`` through historical trading days.

    The controller owns simulator-level concerns only: replay time, auto-settle
    state, market-data refresh for open positions, daily settlement, and replay
    equity snapshots. Business data sources are handled by adapters.
    """

    def __init__(
        self,
        gateway: Any,
        *,
        strategy_name: str,
        is_trade_day: TradeDayPredicate | None = None,
        write_log: LogFunc | None = None,
    ) -> None:
        self.gateway = gateway
        self.strategy_name = strategy_name
        self.is_trade_day = is_trade_day or self._weekday_trade_day
        self.write_log = write_log or self._default_write_log
        self._dynamic_started = False
        self._last_signal_day: date | None = None
        self._auto_settle_previous: bool | None = None

    @staticmethod
    def _weekday_trade_day(day: date) -> bool:
        """Fallback trading calendar used when no real calendar is injected."""
        return day.weekday() < 5

    @staticmethod
    def _default_write_log(message: str) -> None:
        print(message)

    def trade_days(self, start_day: date, end_day: date) -> list[date]:
        """Return inclusive trading days in ``[start_day, end_day]``."""
        if end_day < start_day:
            return []
        days: list[date] = []
        cursor = start_day
        while cursor <= end_day:
            if self.is_trade_day(cursor):
                days.append(cursor)
            cursor += timedelta(days=1)
        return days

    def run_explicit(
        self,
        start_day: date,
        end_day: date,
        adapter: ReplayStrategyAdapter,
    ) -> None:
        """Run an explicit replay window."""
        days = self.trade_days(start_day, end_day)
        if not days:
            self.write_log(f"[replay] {start_day} ~ {end_day} 内无交易日，跳过")
            return

        with self.replay_session():
            self._call_optional(adapter, "prepare", days)
            total = len(days)
            for idx, day in enumerate(days, start=1):
                self.write_log(f"[replay] day {idx}/{total} {day}")
                self.set_replay_now(datetime.combine(day, time(hour=9, minute=30)))
                self._refresh_adapter_symbols(adapter, day)
                self._call_optional(adapter, "before_day", day)
                self._call_optional(adapter, "on_day_open", day)
                self._call_optional(adapter, "before_day_settle", day)
                self.settle_day(day, adapter=adapter)
                self._call_optional(adapter, "on_day_close", day)
            self._call_optional(adapter, "after_replay", days[-1])

    @contextmanager
    def replay_session(self) -> Iterator[None]:
        """Disable simulator auto-settle for the duration of a replay session."""
        self._disable_auto_settle()
        try:
            yield
        finally:
            self.set_replay_now(None)
            self._restore_auto_settle()

    def start_dynamic(self) -> None:
        """Start a dynamic replay session for externally arriving signal days."""
        if self._dynamic_started:
            return
        self._disable_auto_settle()
        self._dynamic_started = True
        self._last_signal_day = None
        self.write_log("[replay] dynamic session started; gateway auto_settle disabled")

    def on_external_signal_day(self, signal_day: date) -> None:
        """Advance settlement before processing a newly observed signal day."""
        self.start_dynamic()
        if self._last_signal_day is not None and signal_day > self._last_signal_day:
            self.advance_to(
                self._last_signal_day,
                signal_day - timedelta(days=1),
            )
        self.set_replay_now(datetime.combine(signal_day, time(hour=9, minute=30)))

    def mark_signal_day(self, signal_day: date) -> None:
        """Record the day of the latest processed external signal."""
        self._last_signal_day = signal_day

    def finalize(self, end_day: date | None = None) -> None:
        """Settle the remaining dynamic replay days and restore simulator state."""
        if not self._dynamic_started:
            return

        try:
            final_day = end_day or self._last_signal_day
            if self._last_signal_day is not None and final_day is not None:
                self.advance_to(self._last_signal_day, final_day)
        finally:
            self._last_signal_day = None
            self._dynamic_started = False
            self.set_replay_now(None)
            self._restore_auto_settle()
            self.write_log("[replay] dynamic session finalized; gateway auto_settle restored")

    def advance_to(self, start_day: date, end_day: date) -> None:
        """Settle every trading day in the inclusive range."""
        days = self.trade_days(start_day, end_day)
        for day in days:
            self.settle_day(day)
        if len(days) > 1:
            self.write_log(
                f"[replay] settled {len(days)} trade days from {start_day} to {end_day}"
            )

    def set_replay_now(self, value: datetime | date | None) -> None:
        """Set simulator logical time for order/trade timestamps."""
        counter = self._counter()
        if counter is None:
            return
        if isinstance(value, datetime) or value is None:
            counter._replay_now = value
        else:
            counter._replay_now = datetime.combine(value, time(hour=9, minute=30))

    def settle_day(
        self,
        day: date,
        *,
        adapter: ReplayStrategyAdapter | None = None,
    ) -> None:
        """Refresh open-position quotes, settle one day, and write equity."""
        counter = self._counter()
        if counter is None:
            return

        self._refresh_position_quotes_for_settle(day)
        try:
            counter.settle_end_of_day(day)
            self.write_log(f"[replay] settle_end_of_day({day}) done")
        except Exception as exc:
            self.write_log(f"[replay] settle_end_of_day({day}) failed: {exc}")
            return

        raw_variables = {"replay_day": str(day)}
        if adapter is not None:
            getter = getattr(adapter, "snapshot_raw_variables", None)
            if callable(getter):
                try:
                    raw = getter(day)
                    if isinstance(raw, dict):
                        raw_variables.update(raw)
                except Exception as exc:
                    self.write_log(f"[replay] snapshot raw variables failed: {exc}")

        self.write_snapshot(day, raw_variables=raw_variables)

    def write_snapshot(
        self,
        day: date,
        *,
        raw_variables: dict[str, Any] | None = None,
    ) -> None:
        """Persist replay equity for one logical trading day."""
        if self.gateway is None:
            return
        try:
            equity, positions_count = calculate_gateway_equity(self.gateway)
            ts = datetime.combine(day, time(hour=15))
            ok = write_replay_snapshot(
                strategy_name=self.strategy_name,
                ts=ts,
                strategy_value=equity,
                account_equity=equity,
                positions_count=positions_count,
                raw_variables=raw_variables,
            )
            if ok and not getattr(self, "_snapshot_logged_first", False):
                self.write_log(
                    f"[replay] replay equity snapshot started day={day} equity={equity:.0f}"
                )
                self._snapshot_logged_first = True
        except Exception as exc:
            self.write_log(f"[replay] write_snapshot({day}) failed: {type(exc).__name__}: {exc}")

    def _counter(self) -> Any | None:
        if self.gateway is None:
            return None
        td = getattr(self.gateway, "td", None)
        return getattr(td, "counter", None)

    def _disable_auto_settle(self) -> None:
        if self.gateway is None:
            return
        try:
            self._auto_settle_previous = bool(
                getattr(self.gateway, "_auto_settle_enabled", True)
            )
            enable = getattr(self.gateway, "enable_auto_settle", None)
            if callable(enable):
                enable(False)
        except Exception as exc:
            self.write_log(f"[replay] disable auto_settle failed: {exc}")

    def _restore_auto_settle(self) -> None:
        if self.gateway is None:
            return
        try:
            enable = getattr(self.gateway, "enable_auto_settle", None)
            if callable(enable):
                enable(True if self._auto_settle_previous is None else self._auto_settle_previous)
        except Exception as exc:
            self.write_log(f"[replay] restore auto_settle failed: {exc}")

    def _refresh_adapter_symbols(
        self,
        adapter: ReplayStrategyAdapter,
        day: date,
    ) -> None:
        getter = getattr(adapter, "refresh_symbols", None)
        if not callable(getter):
            return
        try:
            symbols = getter(day)
        except Exception as exc:
            self.write_log(f"[replay] adapter refresh_symbols failed: {exc}")
            return
        self.refresh_symbols(symbols, day)

    def refresh_symbols(self, symbols: Iterable[str] | None, day: date) -> None:
        """Refresh explicit vt_symbols with the day's historical market data."""
        if not symbols:
            return
        md = getattr(self.gateway, "md", None) if self.gateway is not None else None
        refresh_tick = getattr(md, "refresh_tick", None) if md is not None else None
        if not callable(refresh_tick):
            return
        for vt_symbol in {str(s) for s in symbols if s}:
            try:
                refresh_tick(vt_symbol, as_of_date=day)
            except Exception as exc:
                self.write_log(f"[replay] refresh_tick({vt_symbol}, {day}) failed: {exc}")

    def _refresh_position_quotes_for_settle(self, day: date) -> None:
        counter = self._counter()
        if counter is None:
            return
        positions = getattr(counter, "positions", {})
        if not positions:
            return
        md = getattr(self.gateway, "md", None) if self.gateway is not None else None
        refresh_tick = getattr(md, "refresh_tick", None) if md is not None else None
        if not callable(refresh_tick):
            return

        refreshed = 0
        missed: list[str] = []
        for pos in list(positions.values()):
            vt_symbol = str(getattr(pos, "vt_symbol", "") or "")
            volume = float(getattr(pos, "volume", 0) or 0)
            if not vt_symbol or volume <= 0:
                continue
            try:
                tick = refresh_tick(vt_symbol, as_of_date=day)
                if tick is None:
                    missed.append(vt_symbol)
                    continue
                refreshed += 1
            except Exception as exc:
                missed.append(f"{vt_symbol}:{type(exc).__name__}")
        if refreshed:
            self.write_log(f"[replay] settle quote refresh day={day} positions={refreshed}")
        if missed:
            self.write_log(f"[replay] settle quote missed day={day}: {','.join(missed[:5])}")

    @staticmethod
    def _call_optional(adapter: ReplayStrategyAdapter, name: str, *args: Any) -> Any:
        method = getattr(adapter, name, None)
        if callable(method):
            return method(*args)
        return None
