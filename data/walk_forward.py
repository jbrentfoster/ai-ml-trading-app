"""
Walk-forward validation framework.

Provides time-series-safe train/test splitting and a model-agnostic validator
so that every ML model and strategy built in later steps is evaluated without
look-ahead bias.

Core concepts
-------------
WalkForwardSplit   — generates (train_df, test_df) fold pairs.
compute_metrics    — Sharpe, max drawdown, win rate, annualised return.
WalkForwardValidator — runs a strategy_fn across all folds and aggregates results.

Strategy function contract
--------------------------
Any callable with this signature can be passed to WalkForwardValidator.run():

    def strategy_fn(train_df: pd.DataFrame, test_df: pd.DataFrame) -> pd.Series:
        # train_df : OHLCV + indicator columns, training window
        # test_df  : same columns, held-out test window
        # Returns  : pd.Series of per-bar returns indexed by test_df.index
        ...

The validator is intentionally unopinionated about what happens inside
strategy_fn — it can train an ML model, apply a rule-based signal,
or do anything else, as long as it returns a returns Series.

Window modes
------------
anchored=False (default) — rolling window: train size is fixed, slides forward.
                           Good for capturing recent market regime.
anchored=True            — expanding window: train start is fixed, grows forward.
                           Uses all available history.

Gap / embargo
-------------
gap_bars inserts a buffer of bars between the end of training and the start
of the test window.  Use this to prevent leakage when features have
autocorrelation (e.g. rolling means that overlap the boundary).

Example
-------
    from data.walk_forward import WalkForwardSplit, WalkForwardValidator
    from data.indicators import compute_indicators
    from data.database import get_bars

    df = compute_indicators(get_bars("AAPL", "1d", limit=1000))

    splitter = WalkForwardSplit(n_splits=5, train_bars=252, test_bars=63)
    print(splitter.summary(df).to_string())

    def buy_and_hold(train_df, test_df):
        return test_df["Close"].pct_change().fillna(0)

    validator = WalkForwardValidator(splitter)
    results   = validator.run(df, buy_and_hold)
    print(validator.aggregate(results))
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger("data.walk_forward")

_BARS_PER_YEAR = 252   # trading days; adjust to 52 for weekly, 12 for monthly


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class WalkForwardFold:
    """A single train/test split."""
    fold_index: int
    train_df: pd.DataFrame
    test_df: pd.DataFrame

    @property
    def train_start(self) -> pd.Timestamp:
        return self.train_df.index[0]

    @property
    def train_end(self) -> pd.Timestamp:
        return self.train_df.index[-1]

    @property
    def test_start(self) -> pd.Timestamp:
        return self.test_df.index[0]

    @property
    def test_end(self) -> pd.Timestamp:
        return self.test_df.index[-1]

    def __repr__(self) -> str:
        return (
            f"Fold {self.fold_index}: "
            f"train [{self.train_start.date()} → {self.train_end.date()}] "
            f"({len(self.train_df)} bars)  |  "
            f"test  [{self.test_start.date()} → {self.test_end.date()}] "
            f"({len(self.test_df)} bars)"
        )


@dataclass
class FoldResult:
    """Performance metrics for a single out-of-sample fold."""
    fold_index: int
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    n_bars: int
    total_return: float
    annualized_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    volatility: float
    returns: pd.Series = field(repr=False)   # full return series, for inspection

    def __str__(self) -> str:
        return (
            f"Fold {self.fold_index} [{self.test_start.date()} → {self.test_end.date()}]: "
            f"ret={self.total_return:+.1%}  ann={self.annualized_return:+.1%}  "
            f"sharpe={self.sharpe_ratio:.2f}  mdd={self.max_drawdown:.1%}  "
            f"win={self.win_rate:.1%}"
        )


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(returns: pd.Series, bars_per_year: int = _BARS_PER_YEAR) -> dict:
    """
    Compute performance metrics from a Series of per-bar returns.

    Parameters
    ----------
    returns       : pd.Series of arithmetic returns (e.g. 0.01 = 1% gain).
    bars_per_year : annualisation factor (252 for daily, 52 for weekly, etc.)

    Returns a dict with keys:
        n_bars, total_return, annualized_return, sharpe_ratio,
        max_drawdown, win_rate, volatility
    """
    returns = returns.dropna()
    n = len(returns)

    if n == 0:
        return {
            "n_bars": 0,
            "total_return": 0.0,
            "annualized_return": 0.0,
            "sharpe_ratio": float("nan"),
            "max_drawdown": 0.0,
            "win_rate": float("nan"),
            "volatility": float("nan"),
        }

    cum = (1 + returns).cumprod()
    total_return = float(cum.iloc[-1] - 1)

    # Annualised return via CAGR
    years = n / bars_per_year
    annualized_return = float((1 + total_return) ** (1 / years) - 1) if years > 0 else float("nan")

    # Sharpe (risk-free = 0; annualised)
    mean_r = float(returns.mean())
    std_r  = float(returns.std(ddof=1))
    sharpe_ratio = (
        mean_r / std_r * math.sqrt(bars_per_year)
        if std_r > 0
        else float("nan")
    )

    # Max drawdown
    rolling_max = cum.cummax()
    drawdowns   = (cum - rolling_max) / rolling_max
    max_drawdown = float(drawdowns.min())   # negative number

    # Win rate
    win_rate = float((returns > 0).mean())

    # Annualised volatility
    volatility = std_r * math.sqrt(bars_per_year) if std_r > 0 else 0.0

    return {
        "n_bars":            n,
        "total_return":      total_return,
        "annualized_return": annualized_return,
        "sharpe_ratio":      sharpe_ratio,
        "max_drawdown":      max_drawdown,
        "win_rate":          win_rate,
        "volatility":        volatility,
    }


# ── Splitter ──────────────────────────────────────────────────────────────────

class WalkForwardSplit:
    """
    Generates walk-forward train/test folds from a time-indexed DataFrame.

    Parameters
    ----------
    n_splits   : Number of out-of-sample test periods.
    train_bars : Bars in each training window.
    test_bars  : Bars in each test window.
    gap_bars   : Bars skipped between train end and test start (embargo).
                 Use ≥ 1 when features include rolling statistics that would
                 overlap the train/test boundary.
    anchored   : If False (default), rolling window — train start advances each
                 fold, keeping train_bars constant.
                 If True, expanding window — train always starts from bar 0,
                 growing with each fold.
    """

    def __init__(
        self,
        n_splits: int = 5,
        train_bars: int = 252,
        test_bars: int = 63,
        gap_bars: int = 1,
        anchored: bool = False,
    ) -> None:
        if n_splits < 1:
            raise ValueError("n_splits must be >= 1")
        if train_bars < 1 or test_bars < 1:
            raise ValueError("train_bars and test_bars must be >= 1")
        if gap_bars < 0:
            raise ValueError("gap_bars must be >= 0")

        self.n_splits   = n_splits
        self.train_bars = train_bars
        self.test_bars  = test_bars
        self.gap_bars   = gap_bars
        self.anchored   = anchored

    # ── Minimum required length ───────────────────────────────────────────────

    def min_bars_required(self) -> int:
        """Minimum DataFrame length needed to produce at least one fold."""
        return self.train_bars + self.gap_bars + self.test_bars

    # ── Split ─────────────────────────────────────────────────────────────────

    def split(self, df: pd.DataFrame) -> Iterator[WalkForwardFold]:
        """
        Yield WalkForwardFold objects in chronological order.

        Raises ValueError if df is too short for any split to be produced.
        """
        n = len(df)
        min_req = self.min_bars_required()

        if n < min_req:
            raise ValueError(
                f"DataFrame has {n} bars but walk-forward requires at least "
                f"{min_req} (train={self.train_bars} + gap={self.gap_bars} "
                f"+ test={self.test_bars})."
            )

        # Compute how many folds we can actually produce
        available_for_tests = n - self.train_bars - self.gap_bars
        max_splits = available_for_tests // self.test_bars
        actual_splits = min(self.n_splits, max_splits)

        if actual_splits < self.n_splits:
            log.warning(
                "Requested %d splits but only %d fit in %d bars "
                "(train=%d, gap=%d, test=%d). Using %d splits.",
                self.n_splits, actual_splits, n,
                self.train_bars, self.gap_bars, self.test_bars, actual_splits,
            )

        if actual_splits == 0:
            raise ValueError(
                f"No complete folds fit in {n} bars with the current parameters."
            )

        # Walk-forward fold positions — fold 0 is the earliest test window,
        # fold (actual_splits-1) is the latest (closest to present).
        # The final test window always ends at the last bar of the DataFrame.
        for i in range(actual_splits):
            test_end   = n - (actual_splits - 1 - i) * self.test_bars
            test_start = test_end - self.test_bars
            train_end  = test_start - self.gap_bars

            if self.anchored:
                train_start = 0
            else:
                train_start = max(0, train_end - self.train_bars)

            if train_start >= train_end or test_start >= test_end:
                continue

            yield WalkForwardFold(
                fold_index=i,
                train_df=df.iloc[train_start:train_end].copy(),
                test_df=df.iloc[test_start:test_end].copy(),
            )

    # ── Summary table ─────────────────────────────────────────────────────────

    def summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Return a DataFrame showing each fold's date ranges and bar counts.
        Useful for inspecting the split before running a full validation.
        """
        rows = []
        for fold in self.split(df):
            rows.append({
                "fold":        fold.fold_index,
                "train_start": fold.train_start.date(),
                "train_end":   fold.train_end.date(),
                "train_bars":  len(fold.train_df),
                "gap_bars":    self.gap_bars,
                "test_start":  fold.test_start.date(),
                "test_end":    fold.test_end.date(),
                "test_bars":   len(fold.test_df),
            })
        return pd.DataFrame(rows).set_index("fold")


# ── Validator ─────────────────────────────────────────────────────────────────

class WalkForwardValidator:
    """
    Runs a strategy function across every walk-forward fold and reports
    out-of-sample performance metrics.

    Usage
    -----
        validator = WalkForwardValidator(WalkForwardSplit(n_splits=5))
        results   = validator.run(df, my_strategy_fn)
        print(validator.aggregate(results))
    """

    def __init__(
        self,
        splitter: WalkForwardSplit | None = None,
        bars_per_year: int = _BARS_PER_YEAR,
    ) -> None:
        self.splitter      = splitter or WalkForwardSplit()
        self.bars_per_year = bars_per_year

    def run(
        self,
        df: pd.DataFrame,
        strategy_fn: Callable[[pd.DataFrame, pd.DataFrame], pd.Series],
    ) -> list[FoldResult]:
        """
        Evaluate `strategy_fn` on every fold.

        Parameters
        ----------
        df          : Full historical DataFrame (OHLCV + any features).
                      Must have a DatetimeIndex and enough rows for the splitter.
        strategy_fn : Callable(train_df, test_df) → pd.Series of returns.
                      The returned Series must be indexed by a subset of
                      test_df.index.

        Returns
        -------
        List of FoldResult, one per fold, in chronological order.
        """
        results: list[FoldResult] = []

        for fold in self.splitter.split(df):
            log.debug("Running %s", fold)
            try:
                returns = strategy_fn(fold.train_df, fold.test_df)
            except Exception as exc:
                log.error("strategy_fn raised on fold %d: %s", fold.fold_index, exc)
                raise

            if not isinstance(returns, pd.Series):
                raise TypeError(
                    f"strategy_fn must return a pd.Series, got {type(returns)}"
                )

            metrics = compute_metrics(returns, bars_per_year=self.bars_per_year)
            results.append(FoldResult(
                fold_index=fold.fold_index,
                test_start=fold.test_start,
                test_end=fold.test_end,
                returns=returns,
                **{k: v for k, v in metrics.items() if k != "n_bars"},
                n_bars=metrics["n_bars"],
            ))

            log.info(results[-1])

        return results

    def aggregate(self, results: list[FoldResult]) -> pd.DataFrame:
        """
        Summarise per-fold results into a DataFrame.
        The final row shows the mean across all folds.
        """
        if not results:
            return pd.DataFrame()

        rows = [
            {
                "fold":       r.fold_index,
                "test_start": r.test_start.date(),
                "test_end":   r.test_end.date(),
                "n_bars":     r.n_bars,
                "total_ret":  r.total_return,
                "ann_ret":    r.annualized_return,
                "sharpe":     r.sharpe_ratio,
                "max_dd":     r.max_drawdown,
                "win_rate":   r.win_rate,
            }
            for r in results
        ]
        df = pd.DataFrame(rows).set_index("fold")

        # Append mean row
        numeric = df.select_dtypes(include="number")
        mean_row = numeric.mean().rename("mean")
        summary  = pd.concat([df, mean_row.to_frame().T])

        return summary
