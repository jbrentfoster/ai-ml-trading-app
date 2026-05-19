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


# ── upsert_bars / upsert_indicators overwrite path (in-memory SQLite) ─────────

@pytest.fixture()
def mem_engine(monkeypatch):
    """Replace get_engine() with an in-memory SQLite engine for the test."""
    from sqlalchemy import create_engine
    from data.database import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


class TestUpsertBarsOverwrite:
    """
    Pins the overwrite semantics added 2026-05-19 for the end-of-day refresh
    script.  The default behaviour (skip existing rows) must stay intact; the
    new overwrite=True path must update OHLCV in place so refresh_recent_bars
    can replace mid-day partial bars with the final post-close values.
    """

    def _bar(self, close: float, high: float, low: float):
        return pd.DataFrame(
            {"Open": [close - 0.1], "High": [high], "Low": [low],
             "Close": [close], "Volume": [1_000_000.0]},
            index=pd.DatetimeIndex([datetime(2026, 5, 15)], name="timestamp"),
        )

    def test_default_skips_existing_row(self, mem_engine):
        from data.database import upsert_bars, get_bars

        # First write
        assert upsert_bars(self._bar(100.0, 101.0, 99.0), "TEST", "1d") == 1

        # Second write with different OHLC — should be skipped (return 0)
        assert upsert_bars(self._bar(200.0, 250.0, 50.0), "TEST", "1d") == 0

        stored = get_bars("TEST", "1d")
        assert stored.iloc[-1]["Close"] == 100.0  # original retained
        assert stored.iloc[-1]["Low"]   == 99.0   # original Low retained

    def test_overwrite_updates_existing_row_in_place(self, mem_engine):
        from data.database import upsert_bars, get_bars

        # Seed a mid-day partial bar (the stale_low > true_low scenario from CLAUDE.md)
        assert upsert_bars(self._bar(100.0, 101.0, 98.0), "TEST", "1d") == 1

        # End-of-day refresh: true low is lower (intraday excursion that hit a stop)
        n = upsert_bars(self._bar(99.5, 102.0, 95.0), "TEST", "1d", overwrite=True)
        assert n == 1  # row was UPDATE'd, not inserted

        stored = get_bars("TEST", "1d")
        assert len(stored) == 1
        assert stored.iloc[-1]["Close"] == 99.5   # overwritten
        assert stored.iloc[-1]["High"]  == 102.0  # overwritten
        assert stored.iloc[-1]["Low"]   == 95.0   # overwritten — the canary

    def test_overwrite_inserts_new_rows_too(self, mem_engine):
        """Mixed batch (some existing, some new) — both paths fire under overwrite=True."""
        from data.database import upsert_bars, get_bars

        upsert_bars(self._bar(100.0, 101.0, 99.0), "TEST", "1d")  # seed bar @ 2026-05-15

        # Two-row update: bar 2026-05-15 already exists (UPDATE), bar 2026-05-16 doesn't (INSERT)
        df = pd.DataFrame(
            {"Open": [99.9, 100.5], "High": [102.0, 103.0], "Low": [95.0, 99.0],
             "Close": [99.5, 102.0], "Volume": [1_000_000.0, 1_200_000.0]},
            index=pd.DatetimeIndex(
                [datetime(2026, 5, 15), datetime(2026, 5, 16)], name="timestamp",
            ),
        )
        n = upsert_bars(df, "TEST", "1d", overwrite=True)
        assert n == 2  # 1 update + 1 insert

        stored = get_bars("TEST", "1d")
        assert len(stored) == 2
        # Confirm the older bar was updated, not duplicated
        may15 = stored[stored.index == datetime(2026, 5, 15)]
        assert len(may15) == 1
        assert may15.iloc[0]["Low"] == 95.0


class TestUpsertIndicatorsOverwrite:
    """Same contract for indicator snapshots — refresh script depends on it."""

    def _ind(self, rsi: float):
        return pd.DataFrame(
            {"rsi_14": [rsi], "macd": [0.5], "atr_14": [1.0]},
            index=pd.DatetimeIndex([datetime(2026, 5, 15)], name="timestamp"),
        )

    def test_default_skips_existing_indicator_row(self, mem_engine):
        from data.database import upsert_indicators, get_latest_indicators

        assert upsert_indicators(self._ind(50.0), "TEST", "1d") == 1
        assert upsert_indicators(self._ind(75.0), "TEST", "1d") == 0

        latest = get_latest_indicators("TEST", "1d")
        assert latest is not None and latest["rsi_14"] == 50.0

    def test_overwrite_updates_indicator_row_in_place(self, mem_engine):
        from data.database import upsert_indicators, get_latest_indicators

        upsert_indicators(self._ind(50.0), "TEST", "1d")
        n = upsert_indicators(self._ind(75.0), "TEST", "1d", overwrite=True)
        assert n == 1

        latest = get_latest_indicators("TEST", "1d")
        assert latest is not None and latest["rsi_14"] == 75.0
