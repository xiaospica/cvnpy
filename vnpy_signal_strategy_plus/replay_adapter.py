"""Replay adapters for v2 MySQL signal-journal strategies."""

from __future__ import annotations

import time
import traceback
from datetime import date, datetime
from typing import Any, Callable

from vnpy_qmt_sim.replay import SimReplayController

from .base import APP_NAME
from .signal_journal import StrategySignalApplication, TradeSignalEvent
from .utils import convert_code_to_vnpy_type


TradeDayPredicate = Callable[[date], bool]


class SignalJournalReplayAdapter:
    """Drive a real MySQLSignalStrategyPlus from historical v2 signal events."""

    def __init__(
        self,
        strategy: Any,
        *,
        gateway: Any,
        is_trade_day: TradeDayPredicate | None = None,
        idle_settle_seconds: float = 30.0,
        batch_limit: int = 50,
        final_settle_day: date | None = None,
    ) -> None:
        self.strategy = strategy
        self.strategy_name = str(strategy.strategy_name)
        self.gateway_name = str(strategy.gateway)
        self.idle_settle_seconds = float(idle_settle_seconds)
        self.batch_limit = int(batch_limit)
        self.final_settle_day = final_settle_day
        self.controller = SimReplayController(
            gateway,
            engine=APP_NAME,
            strategy_name=self.strategy_name,
            is_trade_day=is_trade_day,
            write_log=strategy.write_log,
        )
        self._last_signal_day: date | None = None
        self._last_signal_ts = time.time()

    def run_polling_loop(self) -> None:
        """Poll trade_signal_events and replay signals in remark order."""
        self.controller.start_dynamic()
        self.strategy.write_log(
            f"[replay] signal journal adapter started strategy={self.strategy_name} "
            f"gateway={self.gateway_name}"
        )

        try:
            while self.strategy.is_polling_avtive:
                if not self.strategy.Session:
                    time.sleep(self.strategy.poll_interval)
                    continue

                session = None
                try:
                    session = self.strategy.Session()
                    signals = self._query_unconsumed(session)

                    if not signals:
                        self._finalize_after_idle()
                        session.close()
                        time.sleep(max(float(self.strategy.poll_interval), 0.5))
                        continue

                    self._last_signal_ts = time.time()
                    for signal in signals:
                        if not self.strategy.is_polling_avtive:
                            break
                        self._process_one_signal(session, signal)

                    session.close()
                    self.strategy.put_event()

                except Exception as exc:
                    self._set_replay_status("error")
                    self.strategy.write_log(
                        f"[replay] signal journal polling failed: {exc}\n"
                        f"{traceback.format_exc()}"
                    )
                    if session is not None:
                        try:
                            session.rollback()
                            session.close()
                        except Exception:
                            pass
                    time.sleep(1)
        finally:
            try:
                self.controller.finalize(self._effective_final_day())
            finally:
                self._set_replay_status("idle")

    def _query_unconsumed(self, session: Any) -> list[TradeSignalEvent]:
        account_id, gateway_name, engine, strategy_name = self.strategy._application_scope()
        app_join = (
            (StrategySignalApplication.signal_event_id == TradeSignalEvent.id)
            & (StrategySignalApplication.account_id == account_id)
            & (StrategySignalApplication.gateway_name == gateway_name)
            & (StrategySignalApplication.engine == engine)
            & (StrategySignalApplication.strategy_name == strategy_name)
        )
        return (
            session.query(TradeSignalEvent)
            .outerjoin(StrategySignalApplication, app_join)
            .filter(
                TradeSignalEvent.stg == self.strategy_name,
                StrategySignalApplication.id.is_(None),
            )
            .order_by(TradeSignalEvent.remark.asc(), TradeSignalEvent.id.asc())
            .limit(self.batch_limit)
            .all()
        )

    def _finalize_after_idle(self) -> None:
        if self._last_signal_day is None:
            return
        if (time.time() - self._last_signal_ts) < self.idle_settle_seconds:
            return
        try:
            self.controller.finalize(self._effective_final_day())
        finally:
            self._last_signal_day = None
            self._set_replay_status("idle")

    def _effective_final_day(self) -> date | None:
        """Return the day dynamic replay should settle through before finalizing."""
        if self._last_signal_day is None:
            return None
        if self.final_settle_day is None:
            return self._last_signal_day
        if self.final_settle_day < self._last_signal_day:
            return self._last_signal_day
        return self.final_settle_day

    def _process_one_signal(self, session: Any, signal: TradeSignalEvent) -> None:
        signal_day = self._signal_day(signal)
        self._set_replay_status("running")
        self.controller.on_external_signal_day(signal_day)
        self._refresh_signal_symbol(signal, signal_day)
        self.controller.set_replay_now(signal.remark)
        self.strategy.current_dt = signal.remark

        self.strategy._last_signal_orderids = []
        error_msg = None
        try:
            processed = self.strategy.process_signal(signal)
        except Exception as exc:
            self.strategy.write_log(
                f"[replay] process_signal id={signal.id} failed: {exc}\n"
                f"{traceback.format_exc()}"
            )
            processed = False
            error_msg = f"{type(exc).__name__}: {exc}"

        if processed:
            try:
                status = "ordered" if self.strategy._last_signal_orderids else "skipped"
                self.strategy.mark_signal_consumed(
                    session,
                    signal,
                    status=status,
                    error_msg=error_msg,
                )
                session.commit()
            except Exception as exc:
                session.rollback()
                self.strategy.write_log(
                    f"[replay] commit signal application failed id={signal.id}: {exc}"
                )
        else:
            session.rollback()

        self.strategy.last_signal_id = max(self.strategy.last_signal_id, signal.id)
        self._last_signal_day = signal_day
        self.controller.mark_signal_day(signal_day)
        time.sleep(0.01)

    def _set_replay_status(self, status: str) -> None:
        """Expose active replay state so live journal sampling can stay out of batch replay."""
        try:
            setattr(self.strategy, "replay_status", status)
        except Exception:
            pass

    def _refresh_signal_symbol(self, signal: TradeSignalEvent, signal_day: date) -> None:
        try:
            vt_symbol = convert_code_to_vnpy_type(signal.code)
            self.controller.refresh_symbols([vt_symbol], signal_day)
        except Exception as exc:
            self.strategy.write_log(
                f"[replay] refresh signal symbol failed id={signal.id}: {exc}"
            )

    @staticmethod
    def _signal_day(signal: TradeSignalEvent) -> date:
        remark = signal.remark
        if isinstance(remark, datetime):
            return remark.date()
        return remark
