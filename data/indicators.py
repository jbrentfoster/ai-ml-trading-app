"""
Technical indicator computation using the `ta` library.

compute_indicators(df) — pure function, adds columns to a copy of df.
IndicatorEngine        — loads bars from DB, runs compute_indicators, persists results.

Indicators computed:
  Momentum  : RSI (14)
  Trend     : MACD (12/26/9), EMA (9, 21, 50)
  Volatility: Bollinger Bands (20, 2σ), ATR (14)
  Volume    : Volume SMA (20)
"""

from __future__ import annotations

import pandas as pd
import ta

from core.logger import get_logger
from data.database import get_bars, upsert_indicators

log = get_logger("data.indicators")

_MIN_BARS = 52    # enough for EMA-50 + a few warm-up bars
_ATR_WINDOW = 14  # minimum bars required by AverageTrueRange


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append indicator columns to a copy of `df`.

    Input: DataFrame with columns Open, High, Low, Close, Volume
           and a DatetimeIndex.
    Output: same DataFrame with additional indicator columns.
    Rows without enough history will contain NaN — that's expected.
    Returns df with all indicator columns set to NaN if fewer than
    _ATR_WINDOW bars are present (the ta library hard-crashes below that).
    """
    if df.empty:
        return df

    # ta.AverageTrueRange raises IndexError with < window bars — return NaNs.
    if len(df) < _ATR_WINDOW:
        log.warning(
            "Only %d bars available (need >= %d); all indicators set to NaN.",
            len(df), _ATR_WINDOW,
        )
        df = df.copy()
        for col in _INDICATOR_COLS:
            df[col] = float("nan")
        return df

    if len(df) < _MIN_BARS:
        log.warning(
            "Only %d bars available; some indicators need >= %d bars and will be NaN.",
            len(df), _MIN_BARS,
        )

    df = df.copy()

    # ── Momentum ─────────────────────────────────────────────────────────────
    df["rsi_14"] = ta.momentum.RSIIndicator(close=df["Close"], window=14).rsi()

    # ── Trend: MACD ──────────────────────────────────────────────────────────
    macd = ta.trend.MACD(close=df["Close"])           # default 12/26/9
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"]   = macd.macd_diff()

    # ── Trend: EMAs ──────────────────────────────────────────────────────────
    df["ema_9"]  = ta.trend.EMAIndicator(close=df["Close"], window=9).ema_indicator()
    df["ema_21"] = ta.trend.EMAIndicator(close=df["Close"], window=21).ema_indicator()
    df["ema_50"] = ta.trend.EMAIndicator(close=df["Close"], window=50).ema_indicator()

    # ── Volatility: Bollinger Bands ──────────────────────────────────────────
    bb = ta.volatility.BollingerBands(close=df["Close"], window=20, window_dev=2)
    df["bb_upper"]  = bb.bollinger_hband()
    df["bb_middle"] = bb.bollinger_mavg()
    df["bb_lower"]  = bb.bollinger_lband()

    # ── Volatility: ATR ──────────────────────────────────────────────────────
    df["atr_14"] = ta.volatility.AverageTrueRange(
        high=df["High"], low=df["Low"], close=df["Close"], window=14
    ).average_true_range()

    # ── Volume ───────────────────────────────────────────────────────────────
    df["volume_sma_20"] = df["Volume"].rolling(window=20).mean()

    return df


_INDICATOR_COLS = [
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower",
    "ema_9", "ema_21", "ema_50",
    "atr_14", "volume_sma_20",
]


class IndicatorEngine:
    """
    Loads stored bars, computes indicators, and persists them to the DB.

    Usage:
        engine = IndicatorEngine()
        df = engine.run("AAPL", interval="1d")          # returns enriched df
        dfs = engine.run_watchlist(["AAPL", "MSFT"])
    """

    def run(self, symbol: str, interval: str = "1d", limit: int = 500) -> pd.DataFrame:
        """
        Load the most recent `limit` bars → compute all indicators →
        persist new indicator rows → return the enriched DataFrame.
        """
        df = get_bars(symbol, interval, limit=limit)
        if df.empty:
            log.warning(
                "No bars in DB for %s (%s). Run DataFetcher.fetch_symbol() first.",
                symbol, interval,
            )
            return df

        df = compute_indicators(df)

        ind_df = df[[c for c in _INDICATOR_COLS if c in df.columns]]
        n = upsert_indicators(ind_df, symbol, interval)
        log.info("Persisted %d new indicator row(s) for %s (%s)", n, symbol, interval)

        return df

    def run_watchlist(
        self, watchlist: list[str], interval: str = "1d"
    ) -> dict[str, pd.DataFrame]:
        """Run the indicator engine for every symbol in the list."""
        return {sym: self.run(sym, interval=interval) for sym in watchlist}
