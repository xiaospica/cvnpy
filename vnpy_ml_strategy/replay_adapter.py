"""Replay adapter for ``MLStrategyTemplate`` strategies."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

import pandas as pd


class MLStrategyReplayAdapter:
    """Adapt an ML strategy to ``vnpy_qmt_sim.replay.SimReplayController``.

    The adapter preserves the existing ML replay semantics:
    batch inference first, then daily apply; yesterday's prediction drives
    today's open rebalance, and today's prediction is stored for tomorrow.
    """

    def __init__(self, strategy: Any, gateway: Any) -> None:
        self.strategy = strategy
        self.gateway = gateway
        self.strategy_name = str(strategy.strategy_name)
        self.gateway_name = str(strategy.gateway)
        self._days: list[date] = []
        self._total = 0
        self._index = 0
        self._prev_day_pred_score: Optional[pd.Series] = None

    def prepare(self, days: Sequence[date]) -> None:
        """Run batch inference or link upstream selections before replay."""
        self._days = list(days)
        self._total = len(self._days)
        self._index = 0
        self._prev_day_pred_score = None
        if not self._days:
            return

        start = self._days[0]
        end = self._days[-1]
        strategy = self.strategy

        if strategy.signal_source_strategy:
            strategy.write_log(
                f"[replay] 影子策略 signal_source={strategy.signal_source_strategy!r}, "
                "跳过 batch predict, 逐日 link 上游 selections.parquet"
            )
            for day in self._days:
                strategy._link_selections_from_upstream(day)
            return

        need_batch_predict = strategy._need_batch_predict(self._days)
        if not need_batch_predict:
            strategy.write_log("[replay] 已有 batch_mode diagnostics 覆盖整个窗口，跳过批量推理（续跑）")
            return

        strategy.write_log(
            f"[replay] batch predict {start} ~ {end} ({self._total} 交易日)，"
            "spawning 一个推理子进程..."
        )
        try:
            stats = strategy.signal_engine.run_inference_range(
                bundle_dir=strategy.bundle_dir,
                range_start=start,
                range_end=end,
                lookback_days=strategy.lookback_days,
                strategy_name=strategy.strategy_name,
                inference_python=strategy.inference_python,
                output_root=strategy.output_root,
                provider_uri=strategy.provider_uri,
                baseline_path=strategy.baseline_path or None,
                timeout_s=max(3600, self._total * 30),
            )
            strategy.write_log(
                f"[replay] batch predict done: {stats.get('n_days_with_data')} "
                f"days have data of {stats.get('n_days_total')} total "
                f"(returncode={stats.get('returncode')})"
            )
            if stats.get("returncode") != 0:
                strategy.write_log(
                    "[replay] batch subprocess returned non-zero. stderr tail:\n"
                    f"{stats.get('stderr_tail', '')}"
                )
        except Exception as exc:
            strategy.write_log(
                f"[replay] batch predict 异常: {type(exc).__name__}: {exc} — "
                "会逐日 fallback 到已有单日产物"
            )

    def before_day(self, day: date) -> None:
        """Update day index for logging."""
        self._index += 1

    def on_day_open(self, day: date) -> None:
        """Use previous trading day's prediction to rebalance at today's open."""
        strategy = self.strategy
        if self._prev_day_pred_score is None or not strategy.enable_trading:
            return

        try:
            top_candidates = (
                self._prev_day_pred_score.sort_values(ascending=False)
                .head(strategy.topk)
                .index
            )
            candidate_vts: list[str] = []
            for inst in top_candidates:
                vt_symbol = strategy._instrument_to_vt(str(inst))
                if vt_symbol:
                    candidate_vts.append(vt_symbol)

            strategy._refresh_market_data_for_day(day, candidates=candidate_vts)
            stats = strategy.rebalance_to_target(self._prev_day_pred_score, on_day=day)
            strategy.write_log(
                f"[replay] day {self._index}/{self._total} {day} rebalance: "
                f"sells={stats['sells_dispatched']} buys={stats['buys_dispatched']}"
            )
        except Exception as exc:
            strategy.write_log(
                f"[replay] day {day} rebalance 异常 {type(exc).__name__}: {exc}"
            )

    def before_day_settle(self, day: date) -> None:
        """Apply today's predictions before end-of-day settlement."""
        try:
            today_pred_score = self.strategy._replay_apply_day(
                day,
                self._index,
                self._total,
            )
            if today_pred_score is not None and not today_pred_score.empty:
                self._prev_day_pred_score = today_pred_score
        except Exception as exc:
            self.strategy.write_log(
                f"[replay] day {self._index}/{self._total} {day}: apply 异常 "
                f"{type(exc).__name__}: {exc} (continuing)"
            )

    def on_day_close(self, day: date) -> None:
        """Update replay progress after settlement and snapshot."""
        self.strategy.replay_progress = f"{self._index}/{self._total}"
        self.strategy.replay_last_done = day.strftime("%Y-%m-%d")

    def after_replay(self, end_day: date) -> None:
        """Hook kept for protocol completeness."""
        _ = end_day

    def snapshot_raw_variables(self, day: date) -> dict[str, Any]:
        """Return variables persisted with replay equity snapshots."""
        _ = day
        return {
            "replay_status": getattr(self.strategy, "replay_status", ""),
            "replay_progress": getattr(self.strategy, "replay_progress", ""),
        }

    def refresh_symbols(self, day: date) -> list[str]:
        """Refresh current holdings before hooks run."""
        _ = day
        try:
            return list(self.strategy._get_long_positions().keys())
        except Exception:
            return []
