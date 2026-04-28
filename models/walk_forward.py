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
from data.database import log_walk_forward_result
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

            signals, fold_returns, finbert_coverage = self._run_test_window(
                ensemble, fold.train_df, fold.test_df, suppress_finbert=suppress_finbert
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
    ) -> tuple[list[SignalResult], pd.Series, float]:
        """
        Simulate bar-by-bar signal generation on the test window.

        For each bar in test_df, we construct history_df = everything up to
        and including that bar (train + test bars up to i), then call
        ensemble.predict() and gate.evaluate().  This is strictly no-lookahead.

        When suppress_finbert=True, FinBERT is bypassed for every bar in this
        window (test period predates news_available_from) and its weight is
        split equally between LSTM and XGBoost.

        Cost model (corrected — see CLAUDE.md, "WF cost model"):
          * Position is tracked across bars.  BUY → +1, SELL → -1, HOLD →
            keep prior position.  This matches signal_runner.py's live
            semantics (HOLD doesn't flatten existing positions).
          * Cost is charged only on position TRANSITIONS, not on every signal
            bar.  Each transition step is one one-way trade; flipping
            +1 → -1 counts as TWO trades (close long + open short).
          * One-way cost = slippage_pct + commission_per_share / bar_price.
            commission_per_share is a USD amount per share; converting to a
            fractional return requires dividing by price.  Without that
            division the previous code subtracted ~70 bps per signal bar from
            the fractional return — which on liquid large-caps overstates
            commission by 50–1000×.
          * The position is force-flattened on the final bar so trading P&L
            reflects a closed-out strategy.

        Returns (signals, return_series, finbert_coverage) where finbert_coverage
        is the fraction of bars [0, 1] for which FinBERT returned a non-zero score.
        """
        full_df = pd.concat([train_df, test_df])
        signals: list[SignalResult] = []
        returns: list[float]        = []
        finbert_nonzero: int        = 0

        slippage   = config.ml.slippage_pct
        commission = config.ml.commission_per_share

        position = 0   # current position carried into each bar: -1, 0, or +1

        for i, bar_ts in enumerate(test_df.index):
            history_df = full_df.loc[:bar_ts]
            # Pass bar_ts as as_of so FinBERT only uses news available at that
            # point in time — prevents lookahead from today's cached articles.
            as_of = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
            scores = ensemble.predict(history_df, suppress_finbert=suppress_finbert,
                                      as_of=as_of)
            result     = self._gate.evaluate(self._symbol, history_df, scores)
            signals.append(result)

            if scores.get("finbert", 0.0) != 0.0:
                finbert_nonzero += 1

            # Period return: close[i] → close[i+1].  The position decided up to
            # bar i is held during this period.  The last bar has no next
            # close — we force-exit there and realise no further return.
            if i + 1 < len(test_df):
                next_close = test_df["Close"].iloc[i + 1] if "Close" in test_df.columns else 0
                this_close = test_df["Close"].iloc[i]     if "Close" in test_df.columns else 1
                bar_return = (next_close - this_close) / this_close if this_close != 0 else 0.0
            else:
                bar_return = 0.0

            # Target position: BUY/SELL set direction; HOLD keeps prior position.
            if result.signal == "BUY":
                target = 1
            elif result.signal == "SELL":
                target = -1
            else:
                target = position

            # Final bar: force exit so the strategy closes flat at window end.
            if i == len(test_df) - 1:
                target = 0

            # Cost only on transitions.  |Δposition| = number of one-way trades
            # (0 → ±1 = 1 trade; +1 → -1 = 2 trades).
            bar_price = float(test_df["Close"].iloc[i]) if "Close" in test_df.columns else 0.0
            if target != position and bar_price > 0:
                n_trades  = abs(target - position)
                one_way   = slippage + commission / bar_price
                cost      = n_trades * one_way
            else:
                cost = 0.0

            # Realised P&L on this bar uses the position carried INTO the bar.
            bar_pnl = position * bar_return - cost
            returns.append(bar_pnl)
            position = target

        n_bars = len(test_df)
        finbert_coverage = finbert_nonzero / n_bars if n_bars > 0 else 0.0
        return_series = pd.Series(returns, index=test_df.index)
        return signals, return_series, finbert_coverage

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
