"""Replay adapters for MySQL-backed signal strategies."""

from __future__ import annotations

import time
import traceback
from datetime import date, datetime
from typing import Any, Callable

from vnpy_qmt_sim.replay import SimReplayController

from .base import APP_NAME
from .mysql_signal_strategy import Stock
from .utils import convert_code_to_vnpy_type


TradeDayPredicate = Callable[[date], bool]


class StockTradeSignalReplayAdapter:
    """Drive a real ``MySQLSignalStrategyPlus`` from historical ``stock_trade`` rows.

    The adapter keeps business knowledge in ``vnpy_signal_strategy_plus``:
    querying ``stock_trade``, preserving raw signal fields, and invoking
    ``strategy.process_signal``. Simulator day advancement is delegated to
    ``SimReplayController``.
    """

    def __init__(
        self,
        strategy: Any,
        *,
        gateway: Any,
        is_trade_day: TradeDayPredicate | None = None,
        idle_settle_seconds: float = 30.0,
        batch_limit: int = 50,
    ) -> None:
        self.strategy = strategy
        self.strategy_name = str(strategy.strategy_name)
        self.gateway_name = str(strategy.gateway)
        self.idle_settle_seconds = float(idle_settle_seconds)
        self.batch_limit = int(batch_limit)
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
        """Poll ``stock_trade`` and replay signals in remark order."""
        self.controller.start_dynamic()
        self.strategy.write_log(
            f"[replay] stock_trade adapter started strategy={self.strategy_name} "
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
                    signals = (
                        session.query(Stock)
                        .filter(
                            Stock.stg == self.strategy_name,
                            Stock.processed == False,  # noqa: E712
                        )
                        .order_by(Stock.remark.asc(), Stock.id.asc())
                        .limit(self.batch_limit)
                        .all()
                    )

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
                    self.strategy.write_log(
                        f"[replay] stock_trade polling failed: {exc}\n"
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
            self.controller.finalize(self._last_signal_day)

    def _finalize_after_idle(self) -> None:
        if self._last_signal_day is None:
            return
        if (time.time() - self._last_signal_ts) < self.idle_settle_seconds:
            return
        self.controller.finalize(self._last_signal_day)
        self._last_signal_day = None

    def _process_one_signal(self, session: Any, signal: Stock) -> None:
        signal_day = self._signal_day(signal)
        self.controller.on_external_signal_day(signal_day)
        self._refresh_signal_symbol(signal, signal_day)
        self.controller.set_replay_now(signal.remark)
        self.strategy.current_dt = signal.remark

        try:
            processed = self.strategy.process_signal(signal)
        except Exception as exc:
            self.strategy.write_log(
                f"[replay] process_signal id={signal.id} failed: {exc}\n"
                f"{traceback.format_exc()}"
            )
            processed = False

        if processed:
            try:
                signal.processed = True
                session.commit()
            except Exception as exc:
                session.rollback()
                self.strategy.write_log(
                    f"[replay] commit processed=True failed id={signal.id}: {exc}"
                )
        else:
            session.rollback()

        self.strategy.last_signal_id = max(self.strategy.last_signal_id, signal.id)
        self._last_signal_day = signal_day
        self.controller.mark_signal_day(signal_day)
        time.sleep(0.01)

    def _refresh_signal_symbol(self, signal: Stock, signal_day: date) -> None:
        try:
            vt_symbol = convert_code_to_vnpy_type(signal.code)
            self.controller.refresh_symbols([vt_symbol], signal_day)
        except Exception as exc:
            self.strategy.write_log(
                f"[replay] refresh signal symbol failed id={signal.id}: {exc}"
            )

    @staticmethod
    def _signal_day(signal: Stock) -> date:
        remark = signal.remark
        if isinstance(remark, datetime):
            return remark.date()
        return remark
