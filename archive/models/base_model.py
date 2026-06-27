"""
Abstract base class for all ML signal models.

Every concrete model (LSTM, XGBoost, FinBERT) must implement:
  train(train_df)          — fit on a training window
  predict(df)              — return a score in [-1, 1]
  evaluate(test_df)        — return a dict of metrics
  save(path) / load(path)  — serialise / restore model state
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd


class BaseModel(ABC):
    """Shared interface for all signal generation models."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logs and weight history."""

    @abstractmethod
    def train(self, train_df: pd.DataFrame) -> None:
        """
        Fit the model on `train_df`.

        `train_df` is a bar DataFrame with OHLCV columns plus computed
        technical indicators.  The model must not read beyond this window
        (no lookahead).
        """

    @abstractmethod
    def predict(self, df: pd.DataFrame) -> float:
        """
        Return a signal score in [-1.0, 1.0].

          > 0  → bullish (long bias)
          < 0  → bearish (short bias)
            0  → neutral / no signal

        `df` contains the latest available bars up to and including the
        current bar.  The model must only read rows it would have had
        access to at prediction time.
        """

    @abstractmethod
    def evaluate(self, test_df: pd.DataFrame) -> dict:
        """
        Score the model on `test_df`.  Returns a metrics dict that must
        contain at least: {"total_return": float, "sharpe_ratio": float}.
        """

    @abstractmethod
    def save(self, path: str | Path) -> None:
        """Persist model weights/state to `path`."""

    @abstractmethod
    def load(self, path: str | Path) -> None:
        """Restore model weights/state from `path`."""

    # ── Shared utilities ──────────────────────────────────────────────────────

    def _returns_metrics(self, scores: pd.Series, prices: pd.Series) -> dict:
        """
        Compute basic performance metrics from a series of signal scores and
        corresponding close prices.  Long when score > 0, short when < 0.
        """
        import numpy as np

        positions = scores.apply(lambda s: 1 if s > 0 else (-1 if s < 0 else 0))
        price_returns = prices.pct_change().shift(-1).fillna(0)
        strat_returns = positions * price_returns

        total  = (1 + strat_returns).prod() - 1
        mean_r = strat_returns.mean()
        std_r  = strat_returns.std()
        sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else float("nan")

        cum = (1 + strat_returns).cumprod()
        roll_max = cum.cummax()
        drawdown = (cum - roll_max) / roll_max
        max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0

        wins = (strat_returns > 0).sum()
        total_trades = (positions != 0).sum()
        win_rate = float(wins / total_trades) if total_trades > 0 else 0.0

        return {
            "total_return":      float(total),
            "sharpe_ratio":      float(sharpe) if not np.isnan(sharpe) else 0.0,
            "max_drawdown":      max_dd,
            "win_rate":          win_rate,
            "n_trades":          int(total_trades),
        }
