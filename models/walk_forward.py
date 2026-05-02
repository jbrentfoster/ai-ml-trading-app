"""
ML walk-forward orchestrator.

Wraps the three-model ensemble in the walk-forward framework defined in
data/walk_forward.py.  For each fold:
  1. Train ensemble on train window
  2. Generate signals on test window (bar-by-bar, no lookahead)
  3. Evaluate performance
  4. Rebalance ensemble weights
  5. Persist fold metrics to walk_forward_results

After all folds are complete, the ensemble is retrained on the full
dataset for live inference.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.logger import get_logger
from data.walk_forward import WalkForwardSplit
from data.database import log_trades_bulk, log_walk_forward_result
from models.ensemble import EnsembleModel
from models.finbert_model import FinBERTModel
from models.signal_gate import SignalGate, SignalResult
from config.settings import config
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.universe import UniverseSelector

log = get_logger("models.walk_forward")


class MLWalkForwardOrchestrator:
    """
    Drives walk-forward training and evaluation of the full ML pipeline.

    Usage:
        orch = MLWalkForwardOrchestrator(symbol="AAPL")
        results = orch.run(full_df)
        orch.save_models("models/cache/AAPL")
    """

    def __init__(self, symbol: str,
                 universe_selector: UniverseSelector | None = None) -> None:
        cfg = config.ml
        self._symbol   = symbol
        self._splitter = WalkForwardSplit(
            n_splits   = cfg.wf_n_splits,
            train_bars = cfg.wf_train_bars,
            test_bars  = cfg.wf_test_bars,
            gap_bars   = cfg.wf_gap_bars,
        )
        self._gate              = SignalGate()
        self._ensemble: EnsembleModel | None = None
        self._run_id            = str(uuid.uuid4())
        self._universe_selector = universe_selector

    def run(self, df: pd.DataFrame) -> list[dict]:
        """
        Execute walk-forward training.

        Returns a list of fold result dicts (one per fold), each containing
        performance metrics and all SignalResult objects for that fold.
        """
        folds = list(self._splitter.split(df))
        if not folds:
            log.error("No folds generated - dataset too small (%d bars)", len(df))
            return []

        if self._universe_selector is not None:
            log.warning(
                "[%s] SURVIVORSHIP BIAS WARNING: walk-forward is running against a "
                "symbol drawn from a dynamically selected universe. The universe was "
                "determined using data available today, not at the start of each "
                "walk-forward fold. Historical folds may include symbols that only "
                "became candidates in hindsight. For unbiased backtests, use the "
                "static watchlist (--use-watchlist) or a point-in-time universe.",
                self._symbol,
            )

        log.info(
            "[%s] Starting %d-fold walk-forward | run_id=%s",
            self._symbol, len(folds), self._run_id,
        )

        all_results: list[dict] = []

        for fold in folds:
            log.info(
                "Fold %d/%d -- train [%s to %s] | test [%s to %s]",
                fold.fold_index + 1, len(folds),
                fold.train_start.date(), fold.train_end.date(),
                fold.test_start.date(), fold.test_end.date(),
            )

            # Determine whether news data existed at the start of this test window
            suppress_finbert = not FinBERTModel.is_available_for_date(fold.test_start)
            if suppress_finbert:
                log.info(
                    "Fold %d: test window starts %s, before news_available_from (%s) — "
                    "FinBERT suppressed; weight redistributed to LSTM and XGBoost",
                    fold.fold_index + 1,
                    fold.test_start.date(),
                    config.ml.news_available_from,
                )

            ensemble = EnsembleModel(symbol=self._symbol)
            ensemble.train(fold.train_df)

            signals, fold_returns, finbert_coverage, fold_trades = self._run_test_window(
                ensemble, fold.train_df, fold.test_df,
                suppress_finbert=suppress_finbert,
                fold_index=fold.fold_index,
            )
            if fold_trades:
                try:
                    log_trades_bulk(fold_trades)
                except Exception as exc:
                    log.warning(
                        "Fold %d: failed to persist %d trade_log rows: %s",
                        fold.fold_index + 1, len(fold_trades), exc,
                    )
            eval_metrics = ensemble.evaluate(fold.test_df)

            ensemble.rebalance(eval_metrics, finbert_coverage=finbert_coverage)

            perf = self._compute_fold_performance(fold_returns, signals)

            n_bars = len(fold.test_df)
            covered_bars = round(finbert_coverage * n_bars)

            sentiment_note: str | None = None
            if suppress_finbert:
                sentiment_note = (
                    f"suppressed: test window {fold.test_start.date()} precedes "
                    f"news_available_from ({config.ml.news_available_from}); "
                    f"weight redistributed to LSTM+XGBoost"
                )
            elif finbert_coverage < 1.0:
                sentiment_note = (
                    f"coverage: {finbert_coverage:.0%} "
                    f"({covered_bars}/{n_bars} bars had news); "
                    f"FinBERT weight scaled to "
                    f"{config.ml.ensemble_finbert_weight * finbert_coverage:.1%}"
                )
            log.info(
                "Fold %d: FinBERT coverage=%.0f%% (%d/%d bars)",
                fold.fold_index + 1, finbert_coverage * 100, covered_bars, n_bars,
            )

            db_record = {
                "run_id":           self._run_id,
                "symbol":           self._symbol,
                "fold_index":       fold.fold_index,
                "train_start":      fold.train_start,
                "train_end":        fold.train_end,
                "test_start":       fold.test_start,
                "test_end":         fold.test_end,
                "total_return":     perf.get("total_return"),
                "annualized_return": perf.get("annualized_return"),
                "sharpe_ratio":     perf.get("sharpe_ratio"),
                "max_drawdown":     perf.get("max_drawdown"),
                "win_rate":         perf.get("win_rate"),
                "n_signals":        sum(1 for s in signals if s.passed_gate),
                "recorded_at":      datetime.now(timezone.utc).replace(tzinfo=None),
                "sentiment_note":   sentiment_note,
            }
            log_walk_forward_result(db_record)

            all_results.append({**perf, "fold_index": fold.fold_index, "signals": signals})
            log.info("Fold %d complete - Sharpe=%.3f, return=%.2f%%",
                     fold.fold_index + 1,
                     perf.get("sharpe_ratio", 0),
                     perf.get("total_return", 0) * 100)

        # Final model — retrain on full dataset
        log.info("Retraining ensemble on full dataset for live inference ...")
        self._ensemble = EnsembleModel(symbol=self._symbol)
        self._ensemble.train(df)

        return all_results

    def predict(self, df: pd.DataFrame) -> SignalResult:
        """
        Generate a live signal for the latest bar in `df`.
        Requires that `run()` has been called first.
        """
        if self._ensemble is None:
            raise RuntimeError("Call run() before predict()")
        scores = self._ensemble.predict(df)
        return self._gate.evaluate(self._symbol, df, scores)

    def save_models(self, directory: str | Path) -> None:
        if self._ensemble is not None:
            self._ensemble.save(str(directory))

    def load_models(self, directory: str | Path) -> None:
        self._ensemble = EnsembleModel(symbol=self._symbol)
        self._ensemble.load(str(directory))

    # ── Private helpers ───────────────────────────────────────────────────────

    def _run_test_window(
        self,
        ensemble: EnsembleModel,
        train_df: pd.DataFrame,
        test_df:  pd.DataFrame,
        suppress_finbert: bool = False,
        fold_index: int = 0,
    ) -> tuple[list[SignalResult], pd.Series, float, list[dict]]:
        """
        Simulate bar-by-bar signal generation + bracket-order management.

        Phase 4.5 (Phase A) replaced the position-tracking model with explicit
        per-trade bracket simulation.  For each bar, we may:
          1. Execute a pending entry from the prior bar's gate at this bar's Open
             (signal generated on bar t close → enters at bar t+1 open).
          2. Check bracket exits — gap at Open first, then intra-bar (worst-case
             rule: stop wins on a same-bar tie).
          3. Mark-to-market through bars where no event fires.
          4. At close: ATR-based trailing-stop activation and ratchet (the new
             stop level only applies bar t+1 onward; today's intra-bar check
             always uses yesterday's end-of-bar stop).
          5. Signal-flip exit at Close when the gate emits an opposite signal.
          6. Fold-end force-flatten on the last bar regardless of bracket state.

        Resolved design decisions (CLAUDE.md → "Phase 4.5 — Realised P&L plumbing"):
          * Worst-case intra-bar fill (stop wins on a tie) — conservative.
          * Gap-through fills at Open with no extra slippage charge.
          * TP fills are exact (limit at limit-or-better).
          * Stop fills incur `stop_slippage_multiplier × slippage_pct` extra,
            applied only on intra-bar fills (NOT gap fills).
          * No same-bar re-entry after a bracket exit; the gate signal at the
            same bar's close schedules entry on bar t+1.
          * Trailing-stop level is delayed by one bar (today's High does not
            tighten today's stop).

        Closed trades are accumulated in `trades` and returned to the caller
        for bulk persistence to `trade_log` (Phase A schema; entry/exit prices,
        signed P&L, exit reason).

        When `atr_14` is missing or NaN at entry, no bracket levels are set —
        the trade can only exit via signal_flip or fold_end.  This keeps the
        legacy minimal-OHLCV test path functional.

        When suppress_finbert=True, FinBERT is bypassed for every bar in this
        window (test period predates news_available_from) and its weight is
        split equally between LSTM and XGBoost.

        Returns (signals, return_series, finbert_coverage, trades) where
        finbert_coverage is the fraction of bars [0, 1] for which FinBERT
        returned a non-zero score, and `trades` is a list of dicts ready for
        log_trades_bulk().
        """
        full_df = pd.concat([train_df, test_df])
        n_train = len(train_df)
        n_test  = len(test_df)

        cfg_ml         = config.ml
        cfg_risk       = config.risk
        cfg_trading    = config.trading
        slippage       = cfg_ml.slippage_pct
        commission     = cfg_ml.commission_per_share
        stop_slip_mult = cfg_risk.stop_slippage_multiplier
        atr_stop_mult  = cfg_risk.atr_stop_multiplier
        atr_tp_mult    = cfg_risk.atr_take_profit_multiplier
        activation_atr = cfg_risk.trailing_stop_activation_atr
        trail_atr_mult = cfg_risk.trailing_stop_trail_atr
        allow_short    = cfg_trading.allow_short_selling

        has_atr = "atr_14" in test_df.columns

        signals: list[SignalResult] = []
        returns: list[float]        = []
        trades:  list[dict]         = []
        finbert_nonzero: int        = 0

        # ── Position state ────────────────────────────────────────────────────
        position           = 0           # +1 long / -1 short / 0 flat
        entry_px:        float | None = None
        entry_signal:    str   | None = None    # 'BUY' or 'SELL' that opened the trade
        entry_bar_ts                  = None
        stop_px:         float | None = None
        tp_px:           float | None = None
        trail_active                  = False
        peak_px:         float | None = None
        trail_amount:    float | None = None
        pending_entry:   str   | None = None    # 'BUY' or 'SELL' from prior bar
        prev_close:      float | None = None    # for mark-to-market

        recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)

        def _close_trade(
            exit_ts, exit_px_, reason: str, gap_exit_: bool
        ) -> tuple[float, float]:
            """
            Append a trade record to ``trades`` and return
            (exit_only_cost, trade_pnl_pct) for bar-P&L bookkeeping.
            ``exit_only_cost`` is the fractional cost charged to *this bar*
            (entry cost was already charged on the entry bar).
            """
            entry_cost = slippage + commission / entry_px
            exit_cost  = slippage + commission / exit_px_
            if reason in ("stop", "trailing") and not gap_exit_:
                exit_cost += stop_slip_mult * slippage

            gross = (
                (exit_px_ - entry_px) / entry_px if position == 1
                else (entry_px - exit_px_) / entry_px
            )
            total_costs = entry_cost + exit_cost
            pnl_pct = gross - total_costs

            trades.append({
                "source":        "walk_forward",
                "run_id":        self._run_id,
                "fold_index":    fold_index,
                "symbol":        self._symbol,
                "signal":        entry_signal,
                "entry_ts":      entry_bar_ts,
                "entry_px":      float(entry_px),
                "exit_ts":       (exit_ts.to_pydatetime()
                                  if hasattr(exit_ts, "to_pydatetime") else exit_ts),
                "exit_px":       float(exit_px_),
                "exit_reason":   reason,
                "shares":        1.0,
                "pnl":           pnl_pct * float(entry_px),
                "pnl_pct":       pnl_pct,
                "costs_charged": total_costs * float(entry_px),
                "recorded_at":   recorded_at,
            })
            return exit_cost, pnl_pct

        for i, bar_ts in enumerate(test_df.index):
            bar      = test_df.iloc[i]
            try:
                open_px  = float(bar["Open"])
                high_px  = float(bar["High"])
                low_px   = float(bar["Low"])
                close_px = float(bar["Close"])
            except (KeyError, ValueError, TypeError):
                open_px = high_px = low_px = close_px = 0.0

            bar_pnl     = 0.0
            just_entered = False

            # ── 1. Execute pending entry at Open ──────────────────────────
            if pending_entry is not None and position == 0 and open_px > 0:
                entry_px      = open_px
                entry_signal  = pending_entry
                entry_bar_ts  = (bar_ts.to_pydatetime()
                                 if hasattr(bar_ts, "to_pydatetime") else bar_ts)
                position      = 1 if pending_entry == "BUY" else -1

                # ATR from the bar BEFORE entry — strictly no-lookahead.
                atr_for_entry: float | None = None
                if has_atr:
                    bar_idx = n_train + i - 1
                    if 0 <= bar_idx < len(full_df):
                        a = full_df["atr_14"].iloc[bar_idx]
                        if pd.notna(a) and a > 0:
                            atr_for_entry = float(a)

                if atr_for_entry is not None:
                    if position == 1:
                        stop_px = entry_px - atr_stop_mult * atr_for_entry
                        tp_px   = entry_px + atr_tp_mult   * atr_for_entry
                    else:
                        stop_px = entry_px + atr_stop_mult * atr_for_entry
                        tp_px   = entry_px - atr_tp_mult   * atr_for_entry
                else:
                    stop_px = None       # No ATR → no bracket
                    tp_px   = None

                trail_active = False
                peak_px      = None
                trail_amount = None

                # Entry cost (fractional return units, anchored to entry_px)
                entry_cost = slippage + commission / entry_px
                bar_pnl   -= entry_cost
                # Mark-to-market across the entry bar: open → close
                bar_pnl   += position * (close_px - entry_px) / entry_px

                prev_close   = close_px
                just_entered = True
                pending_entry = None

            # ── 2 & 3. Bracket exit checks (in position, brackets armed) ──
            elif position != 0 and stop_px is not None and prev_close is not None:
                exit_px_:    float | None = None
                exit_reason: str   | None = None
                gap_exit                  = False

                # Gap check at Open (gap-through fills at Open without extra slippage)
                if position == 1:
                    if open_px <= stop_px:
                        exit_px_, exit_reason, gap_exit = open_px, ("trailing" if trail_active else "stop"), True
                    elif (not trail_active) and tp_px is not None and open_px >= tp_px:
                        exit_px_, exit_reason, gap_exit = open_px, "tp", True
                else:
                    if open_px >= stop_px:
                        exit_px_, exit_reason, gap_exit = open_px, ("trailing" if trail_active else "stop"), True
                    elif (not trail_active) and tp_px is not None and open_px <= tp_px:
                        exit_px_, exit_reason, gap_exit = open_px, "tp", True

                # Intra-bar — worst-case rule: stop wins on a same-bar tie.
                if exit_reason is None:
                    stop_in_range = low_px <= stop_px <= high_px
                    tp_in_range   = (
                        (not trail_active) and tp_px is not None
                        and low_px <= tp_px <= high_px
                    )
                    if stop_in_range:
                        exit_px_    = stop_px
                        exit_reason = "trailing" if trail_active else "stop"
                    elif tp_in_range:
                        exit_px_    = tp_px
                        exit_reason = "tp"

                if exit_reason is not None:
                    exit_only_cost, _ = _close_trade(bar_ts, exit_px_, exit_reason, gap_exit)
                    bar_pnl += position * (exit_px_ - prev_close) / prev_close - exit_only_cost

                    position     = 0
                    entry_px     = None
                    entry_signal = None
                    entry_bar_ts = None
                    stop_px      = tp_px = peak_px = trail_amount = None
                    trail_active = False
                    prev_close   = close_px
                else:
                    # Held through bar — close-to-close mark-to-market
                    bar_pnl   += position * (close_px - prev_close) / prev_close
                    prev_close = close_px

            elif position != 0 and prev_close is not None:
                # In position but no brackets armed (no ATR available) — MTM only.
                bar_pnl   += position * (close_px - prev_close) / prev_close
                prev_close = close_px

            else:
                # Flat
                prev_close = close_px

            # ── 4. Gate evaluation at this bar's close ────────────────────
            history_df = full_df.loc[:bar_ts]
            as_of = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
            scores = ensemble.predict(history_df, suppress_finbert=suppress_finbert,
                                      as_of=as_of)
            result = self._gate.evaluate(self._symbol, history_df, scores)
            signals.append(result)
            if scores.get("finbert", 0.0) != 0.0:
                finbert_nonzero += 1

            # ── 5. Trail update at close (uses today's atr_14) ────────────
            if position != 0 and entry_px is not None:
                atr_close: float | None = None
                if has_atr:
                    a = test_df["atr_14"].iloc[i]
                    if pd.notna(a) and a > 0:
                        atr_close = float(a)

                if atr_close is not None:
                    if not trail_active:
                        if position == 1:
                            threshold = entry_px + activation_atr * atr_close
                            if close_px >= threshold:
                                trail_active = True
                                peak_px      = high_px
                                trail_amount = trail_atr_mult * atr_close
                                stop_px      = peak_px - trail_amount
                                tp_px        = None    # no TP cap on trailing positions
                        else:
                            threshold = entry_px - activation_atr * atr_close
                            if close_px <= threshold:
                                trail_active = True
                                peak_px      = low_px
                                trail_amount = trail_atr_mult * atr_close
                                stop_px      = peak_px + trail_amount
                                tp_px        = None
                    else:
                        # Ratchet: peak_price monotonic; stop only tightens.
                        # trail_amount stays fixed at the value set on activation.
                        if position == 1 and trail_amount is not None:
                            peak_px  = max(peak_px, high_px)
                            new_stop = peak_px - trail_amount
                            if stop_px is None or new_stop > stop_px:
                                stop_px = new_stop
                        elif position == -1 and trail_amount is not None:
                            peak_px  = min(peak_px, low_px)
                            new_stop = peak_px + trail_amount
                            if stop_px is None or new_stop < stop_px:
                                stop_px = new_stop

            # ── 6. Signal-flip exit at this bar's close ───────────────────
            if position != 0 and result.signal in ("BUY", "SELL") and entry_px is not None:
                opposite = (
                    (position == 1 and result.signal == "SELL")
                    or (position == -1 and result.signal == "BUY")
                )
                if opposite:
                    exit_only_cost, _ = _close_trade(bar_ts, close_px, "signal_flip", False)
                    bar_pnl -= exit_only_cost

                    position     = 0
                    entry_px     = None
                    entry_signal = None
                    entry_bar_ts = None
                    stop_px      = tp_px = peak_px = trail_amount = None
                    trail_active = False

                    # Schedule opposite-direction entry on next bar — but only
                    # if the new direction is allowed.  When allow_short=False
                    # (the live default), a SELL after closing a long is a
                    # close-only operation; we do NOT open a short.  Mirrors
                    # OrderManager.process behaviour in live signal_runner.
                    if result.signal == "BUY" or allow_short:
                        pending_entry = result.signal

            # ── 7. Fresh signal: schedule next-bar entry ──────────────────
            if position == 0 and pending_entry is None and result.signal in ("BUY", "SELL"):
                # Same gate: SELL from flat under long-only is a no-op.
                if result.signal == "BUY" or allow_short:
                    pending_entry = result.signal

            # ── 8. Fold-end force-flatten on last bar ─────────────────────
            if i == n_test - 1 and position != 0 and entry_px is not None:
                exit_only_cost, _ = _close_trade(bar_ts, close_px, "fold_end", False)
                bar_pnl -= exit_only_cost

                position     = 0
                entry_px     = None
                entry_signal = None
                entry_bar_ts = None
                stop_px      = tp_px = peak_px = trail_amount = None
                trail_active = False

            returns.append(bar_pnl)

        finbert_coverage = finbert_nonzero / n_test if n_test > 0 else 0.0
        return_series    = pd.Series(returns, index=test_df.index)
        return signals, return_series, finbert_coverage, trades

    @staticmethod
    def _compute_fold_performance(returns: pd.Series, signals: list[SignalResult]) -> dict:
        """Aggregate performance metrics for one fold."""
        import numpy as np

        if returns.empty or returns.std() == 0:
            return {
                "total_return": 0.0,
                "annualized_return": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
            }

        cum      = (1 + returns).cumprod()
        total_r  = float(cum.iloc[-1] - 1)
        n_bars   = len(returns)
        ann_r    = float((1 + total_r) ** (252 / max(n_bars, 1)) - 1)
        sharpe   = float(returns.mean() / returns.std() * (252 ** 0.5))

        roll_max = cum.cummax()
        drawdown = (cum - roll_max) / roll_max
        max_dd   = float(drawdown.min())

        wins        = (returns > 0).sum()
        total_trades= (pd.Series([s.signal for s in signals]) != "HOLD").sum()
        win_rate    = float(wins / total_trades) if total_trades > 0 else 0.0

        return {
            "total_return":      total_r,
            "annualized_return": ann_r,
            "sharpe_ratio":      sharpe if not np.isnan(sharpe) else 0.0,
            "max_drawdown":      max_dd,
            "win_rate":          win_rate,
        }
