"""
Market data fetcher — downloads OHLCV history via yfinance and stores it in SQLite.

Only bars that are missing from the database are written on each call, so
repeated calls are cheap (incremental updates).

Usage:
    fetcher = DataFetcher()
    df = fetcher.fetch_symbol("AAPL", interval="1d")   # returns stored bars
    all_dfs = fetcher.refresh_watchlist()               # fetch every symbol in config
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from config.settings import config
from core.logger import get_logger
from data.database import get_bars, upsert_bars

log = get_logger("data.fetcher")


class DataFetcher:
    """Downloads and incrementally stores OHLCV bars from Yahoo Finance."""

    def __init__(self) -> None:
        self._cfg = config.data

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_symbol(
        self,
        symbol: str,
        interval: str = "1d",
        days_back: int | None = None,
    ) -> pd.DataFrame:
        """
        Download bars for `symbol`, store any new ones, then return the full
        set of stored bars (not just what was newly fetched).

        Args:
            symbol:    Ticker, e.g. "AAPL".
            interval:  yfinance interval string — "1d", "1h", "5m", etc.
            days_back: How many calendar days of history to request.
                       Defaults to DataConfig.daily_lookback_days for "1d",
                       or DataConfig.intraday_lookback_days for everything else.
        """
        if days_back is None:
            days_back = (
                self._cfg.daily_lookback_days
                if interval == "1d"
                else self._cfg.intraday_lookback_days
            )

        start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        log.debug("Fetching %s | interval=%s | start=%s", symbol, interval, start)

        try:
            raw = yf.Ticker(symbol).history(
                start=start, interval=interval, auto_adjust=True
            )
        except Exception as exc:
            log.error("yfinance fetch failed for %s: %s", symbol, exc)
            return get_bars(symbol, interval)

        if raw.empty:
            log.warning("yfinance returned no data for %s (interval=%s)", symbol, interval)
            return get_bars(symbol, interval)

        df = self._normalise(raw)
        n = upsert_bars(df, symbol, interval)
        # n=0 is the steady-state no-op (data already current); only surface real activity.
        if n > 0:
            log.info("Stored %d new bar(s) for %s (%s)", n, symbol, interval)
        else:
            log.debug("Stored 0 new bar(s) for %s (%s) — already current", symbol, interval)

        return get_bars(symbol, interval, limit=max(days_back * 2, 500))

    def refresh_watchlist(self, interval: str = "1d") -> dict[str, pd.DataFrame]:
        """
        Fetch every symbol in config.data.watchlist.
        Returns {symbol: DataFrame of stored bars}.
        """
        results: dict[str, pd.DataFrame] = {}
        for symbol in self._cfg.watchlist:
            results[symbol] = self.fetch_symbol(symbol, interval=interval)

        log.info(
            "Watchlist refresh complete | symbols=%d | interval=%s",
            len(results), interval,
        )
        return results

    def get_stored_bars(
        self, symbol: str, interval: str = "1d", limit: int = 500
    ) -> pd.DataFrame:
        """Return stored bars from the DB without making a network call."""
        return get_bars(symbol, interval, limit=limit)

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _normalise(df: pd.DataFrame) -> pd.DataFrame:
        """Strip timezone info and keep only OHLCV columns."""
        if df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df[["Open", "High", "Low", "Close", "Volume"]].copy()
