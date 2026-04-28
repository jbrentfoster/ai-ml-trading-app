"""
Unit tests for the Step 2 data pipeline.
No network calls — yfinance and the database are mocked.

Run with:
    python -m pytest tests/test_data_pipeline.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.indicators import compute_indicators


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 100) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with n bars."""
    dates = pd.date_range(
        end=datetime(2024, 1, 1), periods=n, freq="B"
    )
    import numpy as np
    rng = np.random.default_rng(42)
    close = 150 + rng.standard_normal(n).cumsum()
    df = pd.DataFrame(
        {
            "Open":   close - rng.uniform(0, 1, n),
            "High":   close + rng.uniform(0, 2, n),
            "Low":    close - rng.uniform(0, 2, n),
            "Close":  close,
            "Volume": rng.integers(1_000_000, 10_000_000, n).astype(float),
        },
        index=dates,
    )
    df.index.name = "timestamp"
    return df


# ── compute_indicators ────────────────────────────────────────────────────────

class TestComputeIndicators:
    def test_returns_all_expected_columns(self):
        df = compute_indicators(_make_ohlcv(100))
        expected = [
            "rsi_14", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_middle", "bb_lower",
            "ema_9", "ema_21", "ema_50",
            "atr_14", "volume_sma_20",
        ]
        for col in expected:
            assert col in df.columns, f"Missing column: {col}"

    def test_original_columns_preserved(self):
        df = compute_indicators(_make_ohlcv(100))
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            assert col in df.columns

    def test_rsi_bounds(self):
        df = compute_indicators(_make_ohlcv(100))
        rsi = df["rsi_14"].dropna()
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_does_not_mutate_input(self):
        original = _make_ohlcv(100)
        original_cols = list(original.columns)
        compute_indicators(original)
        assert list(original.columns) == original_cols

    def test_empty_df_returns_empty(self):
        result = compute_indicators(pd.DataFrame())
        assert result.empty

    def test_insufficient_bars_returns_mostly_nan(self):
        df = compute_indicators(_make_ohlcv(10))   # below _MIN_BARS
        # RSI needs 14 bars — with 10 bars all RSI values should be NaN
        assert df["rsi_14"].isna().all()


# ── DataFetcher (mocked) ──────────────────────────────────────────────────────

class TestDataFetcher:
    @patch("data.fetcher.yf.Ticker")
    @patch("data.fetcher.upsert_bars", return_value=5)
    @patch("data.fetcher.get_bars")
    def test_fetch_symbol_stores_and_returns_bars(
        self, mock_get_bars, mock_upsert, mock_ticker
    ):
        from data.fetcher import DataFetcher

        sample = _make_ohlcv(30)
        mock_ticker.return_value.history.return_value = sample
        mock_get_bars.return_value = sample

        fetcher = DataFetcher()
        result  = fetcher.fetch_symbol("AAPL", interval="1d")

        mock_upsert.assert_called_once()
        mock_get_bars.assert_called_once()
        assert not result.empty

    @patch("data.fetcher.yf.Ticker")
    @patch("data.fetcher.upsert_bars", return_value=0)
    @patch("data.fetcher.get_bars")
    def test_empty_response_falls_back_to_stored(
        self, mock_get_bars, mock_upsert, mock_ticker
    ):
        from data.fetcher import DataFetcher

        mock_ticker.return_value.history.return_value = pd.DataFrame()
        mock_get_bars.return_value = _make_ohlcv(10)

        fetcher = DataFetcher()
        result  = fetcher.fetch_symbol("AAPL")

        mock_upsert.assert_not_called()
        assert not result.empty


# ── IndicatorEngine (mocked DB) ───────────────────────────────────────────────

class TestIndicatorEngine:
    @patch("data.indicators.upsert_indicators", return_value=10)
    @patch("data.indicators.get_bars")
    def test_run_returns_enriched_df(self, mock_get_bars, mock_upsert):
        from data.indicators import IndicatorEngine

        mock_get_bars.return_value = _make_ohlcv(100)
        engine = IndicatorEngine()
        df = engine.run("AAPL", interval="1d")

        assert "rsi_14" in df.columns
        assert "macd" in df.columns
        mock_upsert.assert_called_once()

    @patch("data.indicators.get_bars")
    def test_run_empty_db_returns_empty(self, mock_get_bars):
        from data.indicators import IndicatorEngine

        mock_get_bars.return_value = pd.DataFrame()
        engine = IndicatorEngine()
        df = engine.run("AAPL")

        assert df.empty
