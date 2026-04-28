"""
Tests for walk-forward validation.

Focus areas:
  - Correct fold counts and bar counts
  - No data leakage (test always strictly after train + gap)
  - Gap/embargo is respected
  - Anchored vs rolling window behaviour
  - compute_metrics correctness
  - Validator runs end-to-end and aggregates cleanly
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from data.walk_forward import (
    WalkForwardSplit,
    WalkForwardValidator,
    compute_metrics,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df(n: int = 500) -> pd.DataFrame:
    """Synthetic daily OHLCV DataFrame."""
    dates = pd.bdate_range(end=datetime(2024, 1, 1), periods=n)
    rng   = np.random.default_rng(0)
    close = 100 + rng.standard_normal(n).cumsum()
    return pd.DataFrame(
        {
            "Open":   close - rng.uniform(0, 0.5, n),
            "High":   close + rng.uniform(0, 1.0, n),
            "Low":    close - rng.uniform(0, 1.0, n),
            "Close":  close,
            "Volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
        },
        index=dates,
    )


def _buy_and_hold(train_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.Series:
    """Trivial strategy: long every bar."""
    return test_df["Close"].pct_change().fillna(0)


# ── WalkForwardSplit ──────────────────────────────────────────────────────────

class TestWalkForwardSplit:

    def test_correct_number_of_folds(self):
        splitter = WalkForwardSplit(n_splits=5, train_bars=100, test_bars=50, gap_bars=0)
        folds = list(splitter.split(_make_df(500)))
        assert len(folds) == 5

    def test_fold_indices_are_sequential(self):
        splitter = WalkForwardSplit(n_splits=4, train_bars=100, test_bars=50)
        indices  = [f.fold_index for f in splitter.split(_make_df(500))]
        assert indices == sorted(indices)

    def test_test_always_after_train(self):
        splitter = WalkForwardSplit(n_splits=5, train_bars=100, test_bars=50, gap_bars=0)
        for fold in splitter.split(_make_df(500)):
            assert fold.train_end < fold.test_start, (
                f"Fold {fold.fold_index}: train_end {fold.train_end} "
                f">= test_start {fold.test_start}"
            )

    def test_gap_is_respected(self):
        gap = 5
        splitter = WalkForwardSplit(n_splits=3, train_bars=100, test_bars=50, gap_bars=gap)
        df    = _make_df(500)
        all_i = list(df.index)
        for fold in splitter.split(df):
            train_end_pos = all_i.index(fold.train_end)
            test_start_pos = all_i.index(fold.test_start)
            actual_gap = test_start_pos - train_end_pos - 1
            assert actual_gap >= gap, (
                f"Fold {fold.fold_index}: expected gap >= {gap}, got {actual_gap}"
            )

    def test_train_size_fixed_in_rolling_mode(self):
        splitter = WalkForwardSplit(n_splits=4, train_bars=100, test_bars=50,
                                    gap_bars=0, anchored=False)
        for fold in splitter.split(_make_df(500)):
            assert len(fold.train_df) == 100, (
                f"Fold {fold.fold_index}: train size {len(fold.train_df)} != 100"
            )

    def test_train_grows_in_anchored_mode(self):
        splitter = WalkForwardSplit(n_splits=4, train_bars=100, test_bars=50,
                                    gap_bars=0, anchored=True)
        folds      = list(splitter.split(_make_df(500)))
        train_sizes = [len(f.train_df) for f in folds]
        assert train_sizes == sorted(train_sizes), "anchored mode: train should grow each fold"

    def test_test_size_consistent(self):
        splitter = WalkForwardSplit(n_splits=5, train_bars=100, test_bars=63)
        for fold in splitter.split(_make_df(700)):
            assert len(fold.test_df) == 63

    def test_no_overlap_between_folds(self):
        splitter = WalkForwardSplit(n_splits=5, train_bars=100, test_bars=50, gap_bars=0)
        folds = list(splitter.split(_make_df(600)))
        for i in range(len(folds) - 1):
            assert folds[i].test_end < folds[i + 1].test_start

    def test_raises_when_df_too_short(self):
        splitter = WalkForwardSplit(n_splits=1, train_bars=300, test_bars=100, gap_bars=0)
        with pytest.raises(ValueError, match="at least"):
            list(splitter.split(_make_df(50)))

    def test_fewer_splits_when_data_insufficient(self):
        # Request 10 folds but data only fits 3
        splitter = WalkForwardSplit(n_splits=10, train_bars=200, test_bars=50, gap_bars=0)
        folds = list(splitter.split(_make_df(400)))
        assert 1 <= len(folds) < 10

    def test_summary_returns_dataframe(self):
        splitter = WalkForwardSplit(n_splits=3, train_bars=100, test_bars=50)
        summary  = splitter.summary(_make_df(500))
        assert isinstance(summary, pd.DataFrame)
        assert len(summary) == 3
        assert "train_start" in summary.columns

    def test_invalid_params_raise(self):
        with pytest.raises(ValueError):
            WalkForwardSplit(n_splits=0)
        with pytest.raises(ValueError):
            WalkForwardSplit(train_bars=0)
        with pytest.raises(ValueError):
            WalkForwardSplit(gap_bars=-1)


# ── compute_metrics ───────────────────────────────────────────────────────────

class TestComputeMetrics:

    def test_empty_series_returns_zeros(self):
        m = compute_metrics(pd.Series([], dtype=float))
        assert m["n_bars"] == 0
        assert m["total_return"] == 0.0

    def test_all_positive_returns(self):
        # Use varying positive returns so std > 0 and Sharpe is well-defined.
        rng = np.random.default_rng(42)
        returns = pd.Series(rng.uniform(0.001, 0.02, 252))
        m = compute_metrics(returns)
        assert m["total_return"] > 0
        assert m["win_rate"] == pytest.approx(1.0)
        assert m["sharpe_ratio"] > 0
        # All returns positive → cumulative return is strictly increasing → no drawdown
        assert m["max_drawdown"] == pytest.approx(0.0, abs=1e-9)

    def test_all_negative_returns(self):
        returns = pd.Series([-0.01] * 252)
        m = compute_metrics(returns)
        assert m["total_return"] < 0
        assert m["win_rate"] == pytest.approx(0.0)
        assert m["max_drawdown"] < 0

    def test_max_drawdown_is_nonpositive(self):
        rng     = np.random.default_rng(1)
        returns = pd.Series(rng.standard_normal(252) * 0.01)
        m       = compute_metrics(returns)
        assert m["max_drawdown"] <= 0

    def test_constant_returns_have_nan_sharpe(self):
        # std = 0 → Sharpe undefined
        returns = pd.Series([0.0] * 100)
        m = compute_metrics(returns)
        assert np.isnan(m["sharpe_ratio"])

    def test_nan_values_are_dropped(self):
        returns = pd.Series([0.01, float("nan"), 0.01, 0.01])
        m = compute_metrics(returns)
        assert m["n_bars"] == 3


# ── WalkForwardValidator ──────────────────────────────────────────────────────

class TestWalkForwardValidator:

    def test_run_returns_correct_fold_count(self):
        splitter  = WalkForwardSplit(n_splits=4, train_bars=100, test_bars=50)
        validator = WalkForwardValidator(splitter)
        results   = validator.run(_make_df(500), _buy_and_hold)
        assert len(results) == 4

    def test_fold_results_have_expected_fields(self):
        splitter  = WalkForwardSplit(n_splits=2, train_bars=100, test_bars=50)
        validator = WalkForwardValidator(splitter)
        results   = validator.run(_make_df(400), _buy_and_hold)
        for r in results:
            assert hasattr(r, "sharpe_ratio")
            assert hasattr(r, "max_drawdown")
            assert hasattr(r, "win_rate")
            assert hasattr(r, "total_return")
            assert isinstance(r.returns, pd.Series)

    def test_aggregate_has_mean_row(self):
        splitter  = WalkForwardSplit(n_splits=3, train_bars=100, test_bars=50)
        validator = WalkForwardValidator(splitter)
        results   = validator.run(_make_df(500), _buy_and_hold)
        agg       = validator.aggregate(results)
        assert "mean" in agg.index.astype(str).tolist()

    def test_strategy_fn_type_error(self):
        splitter  = WalkForwardSplit(n_splits=1, train_bars=100, test_bars=50)
        validator = WalkForwardValidator(splitter)

        def bad_strategy(train_df, test_df):
            return 42   # not a Series

        with pytest.raises(TypeError, match="pd.Series"):
            validator.run(_make_df(300), bad_strategy)

    def test_no_lookahead_in_strategy(self):
        """Verify the strategy cannot accidentally access future prices."""
        seen_test_dates = []

        def recording_strategy(train_df, test_df):
            seen_test_dates.append((train_df.index[-1], test_df.index[0]))
            return test_df["Close"].pct_change().fillna(0)

        splitter  = WalkForwardSplit(n_splits=3, train_bars=100, test_bars=50, gap_bars=1)
        validator = WalkForwardValidator(splitter)
        validator.run(_make_df(500), recording_strategy)

        for train_end, test_start in seen_test_dates:
            assert train_end < test_start, (
                f"Leakage: train_end={train_end} >= test_start={test_start}"
            )


# ── MLWalkForwardOrchestrator cost model ──────────────────────────────────────
#
# These tests target the bug fix in models/walk_forward.py:_run_test_window.
# Pre-fix the function deducted `slippage_pct*2 + commission_per_share` from
# every signal bar's fractional return — but commission_per_share is a
# DOLLAR amount per share, not a fractional cost.  And it charged the full
# round-trip on every signal bar, ignoring position carry-over.
#
# Both bugs are fixed by:
#   1. one_way_cost = slippage + commission/price   (unit fix)
#   2. cost charged only on |Δposition| transitions (round-trip fix)
#   3. position force-flatten on the final bar      (closed-out P&L)
# We drive the orchestrator with mocked ensemble + gate so signal sequences
# are deterministic and the cost arithmetic is the only thing under test.

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from models.signal_gate import SignalResult


def _signal_result(ts, signal: str) -> SignalResult:
    """Minimal SignalResult for one bar."""
    return SignalResult(
        symbol="TEST",
        bar_timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
        signal=signal,
        passed_gate=(signal != "HOLD"),
    )


def _flat_price_df(n_bars: int, price: float = 100.0) -> pd.DataFrame:
    """Return a DataFrame with constant close price — bar_return is exactly 0."""
    dates = pd.bdate_range(end=datetime(2024, 6, 1), periods=n_bars)
    return pd.DataFrame(
        {"Open": price, "High": price, "Low": price, "Close": price, "Volume": 1e6},
        index=dates,
    )


def _build_orchestrator(signals: list[str]):
    """
    Build an MLWalkForwardOrchestrator whose gate.evaluate returns the supplied
    signal sequence in order, and whose ensemble.predict returns finbert=0.

    Returns (orch, mock_ensemble).  The orchestrator's _gate is replaced with
    a MagicMock pre-loaded with side_effect.
    """
    from models.walk_forward import MLWalkForwardOrchestrator
    orch = MLWalkForwardOrchestrator(symbol="TEST")

    # Build a pre-formed list of SignalResult objects — one per bar.
    # We don't know test_df.index here, so use a closure inside the side_effect.
    sig_iter = iter(signals)

    def fake_evaluate(symbol, history_df, scores):
        sig = next(sig_iter)
        return _signal_result(history_df.index[-1], sig)

    orch._gate = MagicMock()
    orch._gate.evaluate.side_effect = fake_evaluate

    ensemble = MagicMock()
    ensemble.predict.return_value = {"lstm": 0.0, "xgb": 0.0, "finbert": 0.0}

    return orch, ensemble


class TestCostModel:
    """Tests for the corrected WF cost model in _run_test_window."""

    def test_all_hold_no_cost_no_returns(self):
        """No signals → no transitions → zero cost, zero returns throughout."""
        orch, ensemble = _build_orchestrator(["HOLD"] * 5)
        df = _flat_price_df(5)
        train_df, test_df = df.iloc[:0], df

        _, returns, _ = orch._run_test_window(ensemble, train_df, test_df)

        assert (returns == 0).all()

    def test_single_buy_then_holds_charges_one_round_trip(self):
        """BUY on bar 0, HOLD afterwards.  Last bar force-exits → 2 trades total
        (entry + final exit), not one per signal bar."""
        from config.settings import config

        # Flat-price test window so any non-zero return is purely cost.
        n_bars = 5
        df = _flat_price_df(n_bars, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD", "HOLD"])

        _, returns, _ = orch._run_test_window(ensemble, train_df, test_df)

        # Exactly two non-zero return entries: entry (bar 0) and forced-exit (bar 4).
        nonzero = (returns != 0).sum()
        assert nonzero == 2, f"expected 2 transition bars, got {nonzero}"

        one_way = config.ml.slippage_pct + config.ml.commission_per_share / 100.0
        assert returns.iloc[0]  == pytest.approx(-one_way)
        assert returns.iloc[-1] == pytest.approx(-one_way)
        # Sum of all returns = total cost paid (entry + exit) on a flat-price strategy.
        assert returns.sum() == pytest.approx(-2 * one_way)

    def test_flip_buy_to_sell_counts_two_trades(self):
        """+1 → -1 transition is two one-way trades (close long + open short)."""
        from config.settings import config

        df = _flat_price_df(3, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "SELL", "HOLD"])

        _, returns, _ = orch._run_test_window(ensemble, train_df, test_df)

        one_way = config.ml.slippage_pct + config.ml.commission_per_share / 100.0
        # Bar 0: 0 → +1 = 1 trade
        # Bar 1: +1 → -1 = 2 trades
        # Bar 2: -1 → 0 (force-exit) = 1 trade
        assert returns.iloc[0] == pytest.approx(-one_way)
        assert returns.iloc[1] == pytest.approx(-2 * one_way)
        assert returns.iloc[2] == pytest.approx(-one_way)

    def test_commission_units_are_fractional_per_price(self):
        """A $1000 stock should incur ~10× less commission cost (as a fraction
        of position value) than a $100 stock — the unit-bug fix."""
        from config.settings import config

        # Same signal sequence, two different price levels.
        signals = ["BUY", "HOLD", "HOLD"]

        df_lo = _flat_price_df(3, price=100.0)
        orch_lo, ens_lo = _build_orchestrator(signals)
        _, returns_lo, _ = orch_lo._run_test_window(ens_lo, df_lo.iloc[:0], df_lo)

        df_hi = _flat_price_df(3, price=1000.0)
        orch_hi, ens_hi = _build_orchestrator(signals)
        _, returns_hi, _ = orch_hi._run_test_window(ens_hi, df_hi.iloc[:0], df_hi)

        # Each window has 2 transitions (entry + final exit).
        # one_way = slippage + commission/price.  Slippage component is the
        # same; commission component scales as 1/price.
        slip = config.ml.slippage_pct
        comm = config.ml.commission_per_share

        expected_lo = -2 * (slip + comm / 100.0)
        expected_hi = -2 * (slip + comm / 1000.0)

        assert returns_lo.sum() == pytest.approx(expected_lo)
        assert returns_hi.sum() == pytest.approx(expected_hi)
        # Higher-priced stock pays strictly less total cost (smaller comm fraction)
        assert returns_hi.sum() > returns_lo.sum()

    def test_held_position_captures_subsequent_bar_returns(self):
        """A long held through HOLD bars should accrue P&L from price moves
        on those HOLD bars — they are not free returns to a flat strategy."""
        n = 4
        # Price doubles by the end of the window, with one move per bar.
        prices = [100.0, 110.0, 121.0, 133.10]
        dates  = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
        df = pd.DataFrame(
            {"Open": prices, "High": prices, "Low": prices,
             "Close": prices, "Volume": [1e6] * n},
            index=dates,
        )
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, returns, _ = orch._run_test_window(ensemble, train_df, test_df)

        # Bar 0: position carried in = 0, no return; pay entry cost.
        # Bar 1: position +1, return = (121-110)/110 ≈ +0.10
        # Bar 2: position +1, return = (133.10-121)/121 ≈ +0.10
        # Bar 3: position +1, return = 0 (last bar) — force exit, pay exit cost.
        # Returns 1 and 2 should be ~+10% (minus zero cost since position unchanged).
        assert returns.iloc[1] == pytest.approx(0.10, rel=1e-3)
        assert returns.iloc[2] == pytest.approx(0.10, rel=1e-3)

        # Net total: cumulative product should be ~ +21% minus ~2 one-way costs.
        from config.settings import config
        avg_cost = config.ml.slippage_pct + config.ml.commission_per_share / 100
        # Ballpark check: total > 18%, total < 22%
        net_total = float((1 + returns).prod() - 1)
        assert 0.18 < net_total < 0.22
