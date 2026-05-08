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


# ── MLWalkForwardOrchestrator cost model + bracket simulation ────────────────
#
# These tests target two distinct concerns of `_run_test_window`:
#   1. Cost-model correctness (one_way = slippage + commission/price; charged
#      on transitions only) — historically buggy, now fixed.
#   2. Bracket simulation (Phase 4.5 — Phase A) — entries deferred to next
#      bar's open, ATR-based stop/tp, gap-aware fills, worst-case rule on
#      same-bar stop+tp tie, trailing-stop activation/ratchet, no same-bar
#      re-entry, fold-end force-flatten.  Closed trades populate `trade_log`.
#
# All tests drive the orchestrator with mocked ensemble + gate so signal
# sequences are deterministic; the simulator's bracket math is the SUT.

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


def _ohlc_df(rows: list[dict], atr: float | None = 2.0) -> pd.DataFrame:
    """
    Synthetic OHLC DataFrame for bracket-simulation tests.

    `rows` is a list of dicts with keys Open/High/Low/Close (any subset
    defaults to the provided Close).  When `atr` is set, every bar gets that
    constant `atr_14` value; pass None to omit the column entirely (no
    brackets armed).
    """
    n = len(rows)
    dates = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
    normalised = []
    for r in rows:
        c = r.get("Close")
        normalised.append({
            "Open":  r.get("Open",  c),
            "High":  r.get("High",  c),
            "Low":   r.get("Low",   c),
            "Close": c,
            "Volume": 1e6,
        })
    df = pd.DataFrame(normalised, index=dates)
    if atr is not None:
        df["atr_14"] = atr
    return df


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
    """Tests for the corrected WF cost model in _run_test_window.

    Entry timing changed under Phase 4.5: a BUY/SELL signal at bar t close
    now schedules entry at bar t+1's open (was: same-bar position change).
    Transition counts and total costs are unchanged; only WHICH bar each cost
    lands on shifted by one.
    """

    def test_all_hold_no_cost_no_returns(self):
        """No signals → no transitions → zero cost, zero returns throughout."""
        orch, ensemble = _build_orchestrator(["HOLD"] * 5)
        df = _flat_price_df(5)
        train_df, test_df = df.iloc[:0], df

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        assert (returns == 0).all()
        assert trades == []

    def test_single_buy_then_holds_charges_one_round_trip(self):
        """BUY on bar 0 → entry on bar 1 open; force-exit on last bar.
        Two cost-bearing bars (entry + fold_end), not one per signal bar."""
        from config.settings import config

        # Flat-price test window so any non-zero return is purely cost.
        n_bars = 5
        df = _flat_price_df(n_bars, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD", "HOLD"])

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        # Two non-zero return entries: entry (bar 1) and force-exit (bar 4).
        nonzero = (returns != 0).sum()
        assert nonzero == 2, f"expected 2 cost bars, got {nonzero}"

        one_way = config.ml.slippage_pct + config.ml.commission_per_share / 100.0
        assert returns.iloc[0]  == pytest.approx(0.0)        # signal bar — no cost yet
        assert returns.iloc[1]  == pytest.approx(-one_way)   # entry bar
        assert returns.iloc[-1] == pytest.approx(-one_way)   # force-exit bar
        # Sum of all returns = total cost paid (entry + exit) on a flat-price strategy.
        assert returns.sum() == pytest.approx(-2 * one_way)
        # One trade logged with exit_reason='fold_end'.
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "fold_end"

    def test_flip_buy_to_sell_counts_two_trades(self, monkeypatch):
        """BUY then SELL with shorts allowed: bar 1 enters long & immediately
        signal-flips at close, bar 2 enters short & is force-flattened at close.
        Total cost = 4 one-ways.

        Requires `allow_short_selling=True` — under the default (False),
        the SELL after closing the long is a no-op (see
        `test_long_only_sell_does_not_open_short` below)."""
        from config.settings import config
        monkeypatch.setattr(config.trading, "allow_short_selling", True)

        df = _flat_price_df(3, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "SELL", "HOLD"])

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        one_way = config.ml.slippage_pct + config.ml.commission_per_share / 100.0
        # Bar 0: BUY signal — schedule entry; no cost yet.
        # Bar 1: enter long at open; SELL signal at close → signal-flip exit.
        #        Two one-ways on this bar (entry + signal-flip).  Pending=SELL for bar 2.
        # Bar 2: enter short at open; last bar → force-flatten at close.
        #        Two one-ways on this bar (entry + fold_end).
        assert returns.iloc[0] == pytest.approx(0.0)
        assert returns.iloc[1] == pytest.approx(-2 * one_way)
        assert returns.iloc[2] == pytest.approx(-2 * one_way)
        assert returns.sum()    == pytest.approx(-4 * one_way)
        # Two trades logged: signal_flip on the long, fold_end on the short.
        assert [t["exit_reason"] for t in trades] == ["signal_flip", "fold_end"]
        assert [t["signal"]      for t in trades] == ["BUY", "SELL"]

    def test_long_only_sell_from_flat_is_noop(self, monkeypatch):
        """With allow_short_selling=False (default): a SELL signal at flat
        position must NOT open a short.  No entry, no costs, no trade record.

        Mirrors OrderManager.process behaviour in live signal_runner — a SELL
        with no existing long is REJECTED_NO_POSITION, never a short open."""
        from config.settings import config
        monkeypatch.setattr(config.trading, "allow_short_selling", False)

        df = _flat_price_df(3, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["SELL", "SELL", "HOLD"])

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        # No position ever opened — every bar's bar_pnl is 0, no trades logged.
        assert (returns == 0).all()
        assert trades == []

    def test_long_only_sell_after_long_closes_without_reopening_short(self, monkeypatch):
        """With allow_short_selling=False: BUY then SELL closes the long via
        signal_flip but does NOT schedule a short entry on the next bar.
        Exactly one trade, exit_reason='signal_flip', no fold_end short."""
        from config.settings import config
        monkeypatch.setattr(config.trading, "allow_short_selling", False)

        df = _flat_price_df(3, price=100.0)
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "SELL", "HOLD"])

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        one_way = config.ml.slippage_pct + config.ml.commission_per_share / 100.0
        # Bar 0: BUY signal scheduled.
        # Bar 1: enter long at open; SELL signal at close → signal_flip exit.
        #        Two one-ways on this bar (entry + signal-flip).
        #        Pending entry NOT scheduled (long-only).
        # Bar 2: flat — no costs.
        assert returns.iloc[0] == pytest.approx(0.0)
        assert returns.iloc[1] == pytest.approx(-2 * one_way)
        assert returns.iloc[2] == pytest.approx(0.0)
        assert returns.sum()    == pytest.approx(-2 * one_way)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "signal_flip"
        assert trades[0]["signal"]      == "BUY"

    def test_commission_units_are_fractional_per_price(self):
        """A $1000 stock should incur ~10× less commission cost (as a fraction
        of position value) than a $100 stock — the unit-bug fix."""
        from config.settings import config

        # Same signal sequence, two different price levels.
        signals = ["BUY", "HOLD", "HOLD"]

        df_lo = _flat_price_df(3, price=100.0)
        orch_lo, ens_lo = _build_orchestrator(signals)
        _, returns_lo, _, _ = orch_lo._run_test_window(ens_lo, df_lo.iloc[:0], df_lo)

        df_hi = _flat_price_df(3, price=1000.0)
        orch_hi, ens_hi = _build_orchestrator(signals)
        _, returns_hi, _, _ = orch_hi._run_test_window(ens_hi, df_hi.iloc[:0], df_hi)

        # Each window has 2 fills (entry on bar 1 + force-exit on bar 2).
        # one_way = slippage + commission/price.  Slippage is constant; the
        # commission component scales as 1/price.
        slip = config.ml.slippage_pct
        comm = config.ml.commission_per_share

        expected_lo = -2 * (slip + comm / 100.0)
        expected_hi = -2 * (slip + comm / 1000.0)

        assert returns_lo.sum() == pytest.approx(expected_lo)
        assert returns_hi.sum() == pytest.approx(expected_hi)
        # Higher-priced stock pays strictly less total cost (smaller comm fraction)
        assert returns_hi.sum() > returns_lo.sum()

    def test_held_position_captures_subsequent_bar_returns(self):
        """A long held through HOLD bars accrues P&L from price moves on
        those HOLD bars — not free returns to a flat strategy.

        With next-bar entry: BUY at bar 0 close → enter at bar 1 open=110.
        Returns from bar 2 onward are mark-to-market vs. prior close.
        """
        n = 4
        # Price increases by the end of the window, with one move per bar.
        prices = [100.0, 110.0, 121.0, 133.10]
        dates  = pd.bdate_range(end=datetime(2024, 6, 1), periods=n)
        df = pd.DataFrame(
            {"Open": prices, "High": prices, "Low": prices,
             "Close": prices, "Volume": [1e6] * n},
            index=dates,
        )
        train_df, test_df = df.iloc[:0], df
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, returns, _, trades = orch._run_test_window(ensemble, train_df, test_df)

        # Bar 0: signal BUY — pending entry; no cost.
        # Bar 1: enter at 110. Open==Close==110, MTM=0; pay entry cost.
        # Bar 2: HELD; MTM = (121-110)/110 ≈ +0.10. No cost.
        # Bar 3: last bar; force-flatten at 133.10. MTM = (133.10-121)/121 ≈ +0.10
        #        less exit cost.
        assert returns.iloc[2] == pytest.approx(0.10, rel=1e-3)

        # Net total: cumulative product should be ~ +21% minus ~2 one-way costs.
        net_total = float((1 + returns).prod() - 1)
        assert 0.18 < net_total < 0.22
        # One trade closed at fold_end with positive pnl_pct.
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "fold_end"
        assert trades[0]["pnl_pct"] > 0


class TestBracketSimulation:
    """Tests for Phase 4.5 (Phase A) bracket simulation in _run_test_window."""

    def test_stop_caps_loss_at_atr_distance(self):
        """ATR=2, stop_mult=2 → stop=entry-4.  A bar with Low<=stop fires
        the stop intra-bar and exits at stop_px with extra slippage."""
        from config.settings import config

        # Bar 0: signal-only (BUY).
        # Bar 1: enter long at Open=100.  stop=96, tp=106.
        # Bar 2: Low=95 → intra-bar stop fires at 96.
        # Bar 3: flat, no further trades.
        rows = [
            {"Close": 100},                                  # bar 0 (signal bar)
            {"Open": 100, "High": 101, "Low": 99,  "Close": 100},  # bar 1 (entry)
            {"Open": 100, "High": 101, "Low": 95,  "Close": 99},   # bar 2 (stop hit)
            {"Open": 99,  "High": 99,  "Low": 99,  "Close": 99},   # bar 3 (flat)
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "stop"
        assert t["entry_px"]    == pytest.approx(100.0)
        assert t["exit_px"]     == pytest.approx(96.0)   # stop_px, not Low

        # Loss is bounded by ATR distance plus one round-trip + intra-bar stop slippage.
        slip = config.ml.slippage_pct
        comm = config.ml.commission_per_share
        stop_slip = config.risk.stop_slippage_multiplier * slip
        expected_pnl_pct = (96 - 100) / 100 - (slip + comm / 100) - (slip + comm / 96) - stop_slip
        assert t["pnl_pct"] == pytest.approx(expected_pnl_pct, abs=1e-6)

    def test_tp_locks_gain_at_atr_distance(self):
        """ATR=2, tp_mult=3 → tp=entry+6.  TP fills exactly with NO slippage."""
        from config.settings import config

        rows = [
            {"Close": 100},                                          # bar 0
            {"Open": 100, "High": 101, "Low": 99,  "Close": 100},    # bar 1 (entry)
            {"Open": 100, "High": 110, "Low": 100, "Close": 109},    # bar 2 (tp hit)
            {"Open": 109, "High": 109, "Low": 109, "Close": 109},    # bar 3
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "tp"
        assert t["exit_px"]     == pytest.approx(106.0)

        slip = config.ml.slippage_pct
        comm = config.ml.commission_per_share
        # TP fills exact: no stop-slippage extra.
        expected_pnl_pct = (106 - 100) / 100 - (slip + comm / 100) - (slip + comm / 106)
        assert t["pnl_pct"] == pytest.approx(expected_pnl_pct, abs=1e-6)

    def test_gap_through_stop_fills_at_open_no_extra_slippage(self):
        """Open <= stop (long) → fill at Open, no stop-slippage charge.
        The gap IS the slippage; don't double-count."""
        from config.settings import config

        rows = [
            {"Close": 100},                                          # bar 0
            {"Open": 100, "High": 101, "Low": 99,  "Close": 100},    # bar 1 (entry; stop=96)
            {"Open":  90, "High":  92, "Low":  85, "Close":  88},    # bar 2 (gap-down through stop)
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "stop"
        assert t["exit_px"]     == pytest.approx(90.0)   # Open, not stop_px=96 nor Low=85

        # No extra stop-slippage on gap fills.
        slip = config.ml.slippage_pct
        comm = config.ml.commission_per_share
        expected_pnl_pct = (90 - 100) / 100 - (slip + comm / 100) - (slip + comm / 90)
        assert t["pnl_pct"] == pytest.approx(expected_pnl_pct, abs=1e-6)

    def test_both_touched_bar_fills_stop_not_tp(self):
        """Worst-case rule: when both stop and tp are touched on the same bar,
        the stop fills (TP is forfeit)."""
        rows = [
            {"Close": 100},                                          # bar 0
            {"Open": 100, "High": 101, "Low":  99, "Close": 100},    # bar 1 (entry)
            # Bar 2: full range crosses both stop=96 and tp=106 — worst case.
            {"Open": 100, "High": 107, "Low":  95, "Close": 100},    # bar 2 (both)
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "stop"
        assert t["exit_px"]     == pytest.approx(96.0)

    def test_trailing_activates_then_triggers(self):
        """Close >= entry + activation_atr × ATR → activate trail at peak-trail_amount.
        A subsequent down-move below the trail level fires exit_reason='trailing'.

        ATR=2: stop=96, tp=106, activation_dist=4, trail_dist=4.
        Activation bar must satisfy close>=104 AND high<106 (else TP fires
        intra-bar before activation runs).
        """
        rows = [
            {"Close": 100},                                              # bar 0
            {"Open": 100, "High": 101, "Low":  99,    "Close": 100},     # bar 1 (entry)
            {"Open": 100, "High": 105, "Low": 100,    "Close": 104.5},   # bar 2 activate (high<106, close>=104) → peak=105, trail=101
            {"Open": 104, "High": 104, "Low": 100,    "Close": 101},     # bar 3 trail trigger at 101
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "trailing"
        # Activation on bar 2: peak=105, stop=105-4=101.  On bar 3 low=100<=101 → fill at 101.
        assert t["exit_px"] == pytest.approx(101.0)

    def test_trailing_ratchet_locks_in_gains(self):
        """Trail level monotonically tightens as peak rises.  Without the
        ratchet, the trail set at activation (101) would let bar 4 Low=104
        ride; with it, the ratcheted stop (106) fills at the higher level."""
        rows = [
            {"Close": 100},                                              # bar 0
            {"Open": 100, "High": 101, "Low":  99,    "Close": 100},     # bar 1 entry
            {"Open": 100, "High": 105, "Low": 100,    "Close": 104.5},   # bar 2 activate, peak=105, trail=101
            {"Open": 104, "High": 110, "Low": 102,    "Close": 108},     # bar 3 ratchet (tp_px now None) peak=110, trail=106
            {"Open": 107, "High": 107, "Low": 104,    "Close": 105},     # bar 4 trail trigger at 106
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "trailing"
        # Without ratcheting, stop would still be 101 and bar 4 Low=104 wouldn't fire.
        # With ratcheting the stop is at 106 → fills at 106.
        assert t["exit_px"] == pytest.approx(106.0)

    def test_trailing_one_bar_delay(self):
        """The new trailing-stop level set at bar t close only applies bar t+1+.
        Today's intra-bar check always uses yesterday's end-of-bar stop level.

        Bar 2 activates with close=104.5; new trail level = 101.  Bar 2's
        intra-bar check uses the OLD stop (96), so bar 2 Low=97 (below the
        new trail of 101 but above the old stop 96) does NOT exit.
        Bar 3 then uses the NEW trail (101) and Low=100 fills at 101.
        """
        rows = [
            {"Close": 100},                                              # bar 0
            {"Open": 100, "High": 101, "Low":  99,    "Close": 100},     # bar 1 (entry)
            {"Open": 100, "High": 105, "Low":  97,    "Close": 104.5},   # bar 2 activate, but no exit
            {"Open": 102, "High": 104, "Low": 100,    "Close": 102},     # bar 3 (trail at 101)
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        # If today's High had tightened today's stop, bar 2 Low=97 vs new-stop 101
        # would have exited there — that's the lookahead we avoid.
        assert t["exit_reason"] == "trailing"
        assert t["exit_px"]     == pytest.approx(101.0)
        assert t["exit_ts"]     == df.index[3].to_pydatetime()

    def test_no_same_bar_re_entry_after_stop(self):
        """When a stop fires on bar t, a fresh BUY signal at bar t close
        schedules entry on bar t+1 — never the same bar as the bracket exit."""
        rows = [
            {"Close": 100},                                          # bar 0 (1st BUY)
            {"Open": 100, "High": 101, "Low":  99, "Close": 100},    # bar 1 (entry)
            {"Open": 100, "High": 100, "Low":  95, "Close":  97},    # bar 2 (stop @ 96)
            {"Open":  98, "High":  99, "Low":  97, "Close":  98},    # bar 3 (re-entry)
            {"Open":  98, "High":  99, "Low":  97, "Close":  98},    # bar 4 (force-exit)
        ]
        df = _ohlc_df(rows, atr=2.0)
        # Signal sequence: BUY at bar 0 (first entry), BUY again at bar 2 close
        # (after stop) → must enter bar 3, never same-bar bar 2.
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "BUY", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 2
        # First trade: bar 1 entry, bar 2 stop.
        assert trades[0]["exit_reason"] == "stop"
        assert trades[0]["entry_ts"]    == df.index[1].to_pydatetime()
        assert trades[0]["exit_ts"]     == df.index[2].to_pydatetime()
        # Second trade: bar 3 entry — NOT bar 2 (no same-bar re-entry).
        assert trades[1]["exit_reason"] == "fold_end"
        assert trades[1]["entry_ts"]    == df.index[3].to_pydatetime()

    def test_fold_end_force_flatten(self):
        """A position still open at the last bar is force-closed at last bar's
        Close with exit_reason='fold_end'."""
        rows = [
            {"Close": 100},                                          # bar 0
            {"Open": 100, "High": 102, "Low":  99, "Close": 101},    # bar 1 (entry)
            {"Open": 101, "High": 102, "Low": 100, "Close": 102},    # bar 2 (held; no bracket trigger)
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(ensemble, df.iloc[:0], df)

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "fold_end"
        assert t["exit_px"]     == pytest.approx(102.0)
        assert t["exit_ts"]     == df.index[-1].to_pydatetime()

    def test_trade_log_record_fields_populated(self):
        """Each trade row must have the schema fields populated for log_trades_bulk."""
        rows = [
            {"Close": 100},
            {"Open": 100, "High": 101, "Low":  99, "Close": 100},
            {"Open": 100, "High": 101, "Low":  95, "Close":  99},
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        _, _, _, trades = orch._run_test_window(
            ensemble, df.iloc[:0], df, fold_index=3,
        )

        assert len(trades) == 1
        t = trades[0]
        # Schema check — every column expected by data.database.TradeLog.
        for k in ("source", "run_id", "fold_index", "symbol", "signal",
                  "entry_ts", "entry_px", "exit_ts", "exit_px", "exit_reason",
                  "shares", "pnl", "pnl_pct", "costs_charged", "recorded_at"):
            assert k in t, f"missing field: {k}"
        assert t["source"]     == "walk_forward"
        assert t["fold_index"] == 3
        assert t["symbol"]     == "TEST"
        assert t["signal"]     == "BUY"
        # Phase C: Kelly-sized position; shares >= 1 (cold-start fallback path
        # still produces a positive share count at typical test prices).
        assert t["shares"]     >= 1
        # pnl and costs_charged are dollar-denominated and scale with shares.
        assert t["pnl"]            == pytest.approx(t["pnl_pct"] * t["entry_px"] * t["shares"])
        assert t["costs_charged"]  > 0

    def test_realised_kelly_history_drives_trade_shares(self):
        """Phase C: when ``kelly_history`` carries enough trades and a positive
        ``f_star``, ``_run_test_window`` sizes positions via realised Kelly —
        the resulting ``trades[*]['shares']`` matches what PositionSizer would
        return given the same kelly_history input.

        This is the primary integration test for the WF wiring; it isolates
        the sizer hookup without depending on whatever happens to live in
        the production trade_log table.
        """
        from config.settings import config
        from risk.position_sizer import PositionSizer

        rows = [
            {"Close": 100},
            {"Open": 100, "High": 102, "Low":  99, "Close": 101},   # bar 1 entry
            {"Open": 101, "High": 102, "Low": 100, "Close": 102},   # bar 2 held
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        # 60% wins at +3%, 40% losses at -2% → b=1.5, f*=(0.6*1.5-0.4)/1.5≈0.333.
        kelly_history = {
            "n_trades":     max(config.risk.min_trades_for_realised_kelly, 30),
            "win_rate":     0.60,
            "avg_win_pct":  0.03,
            "avg_loss_pct": 0.02,
            "b":            1.5,
            "f_star":       (0.60 * 1.5 - 0.40) / 1.5,
        }

        # Predict what PositionSizer would do at entry (bar 1 open=100).
        expected = PositionSizer().calculate(
            symbol="TEST",
            signal="BUY",
            equity=float(config.trading.paper_equity),
            entry_price=100.0,
            atr=2.0,
            kelly_history=kelly_history,
        )

        _, _, _, trades = orch._run_test_window(
            ensemble, df.iloc[:0], df, kelly_history=kelly_history,
        )

        assert len(trades) == 1
        t = trades[0]
        # The sizer claimed kelly_realised at this kelly_history, and the
        # trade row should reflect those Kelly-sized shares.
        assert expected.method == "kelly_realised"
        assert expected.shares >= 1
        assert t["shares"]     == pytest.approx(float(expected.shares))
        # Dollar P&L scales with the Kelly-sized share count.
        assert t["pnl"]            == pytest.approx(t["pnl_pct"] * t["entry_px"] * t["shares"])
        assert t["costs_charged"]  > 0

    def test_zero_share_kelly_skips_entry(self):
        """Phase C: when PositionSizer would return ``shares < 1`` (e.g.
        Kelly says abstain or notional is too small), ``_run_test_window``
        skips the entry rather than opening a phantom 0-share trade.

        We force this by patching ``self._sizer.calculate`` to always return
        a 0-share PositionSize; the simulator should then log no trades
        and produce zero per-bar P&L throughout the window.
        """
        from risk.position_sizer import PositionSize

        rows = [
            {"Close": 100},
            {"Open": 100, "High": 102, "Low":  99, "Close": 101},
            {"Open": 101, "High": 102, "Low": 100, "Close": 102},
        ]
        df = _ohlc_df(rows, atr=2.0)
        orch, ensemble = _build_orchestrator(["BUY", "HOLD", "HOLD"])

        zero_size = PositionSize(
            symbol="TEST", signal="BUY", shares=0,
            entry_price=100.0, stop_price=96.0, take_profit_price=106.0,
            position_value=0.0, position_pct=0.0,
            kelly_fraction_used=0.0, method="kelly_realised",
        )
        orch._sizer = MagicMock()
        orch._sizer.calculate.return_value = zero_size

        _, returns, _, trades = orch._run_test_window(
            ensemble, df.iloc[:0], df,
        )

        assert trades == []
        assert (returns == 0).all()
