"""
Market regime detection.

Classifies the current market into one of three regimes:
  TRENDING        — strong directional trend (high ADX, MA aligned)
  MEAN_REVERTING  — low ADX, price oscillating near mean
  HIGH_VOLATILITY — VIX above threshold or large ATR relative to price

VIX data is fetched via yfinance and cached in the ohlcv_bars table
(symbol="^VIX", interval="1d") so it is available offline after the
first run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

import pandas as pd
import yfinance as yf

from core.logger import get_logger
from data.database import get_bars, upsert_bars

log = get_logger("models.regime")

_VIX_SYMBOL        = "^VIX"
_VIX_HIGH_THRESHOLD = 25.0   # VIX above this → HIGH_VOLATILITY
_ADX_TRENDING       = 25.0   # ADX above this → TRENDING
_VIX_CACHE_TTL_HOURS = 4


class RegimeType(Enum):
    TRENDING        = "TRENDING"
    MEAN_REVERTING  = "MEAN_REVERTING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


class RegimeDetector:

    def detect(self, df: pd.DataFrame) -> RegimeType:
        """
        Detect market regime from a bar DataFrame that already contains
        technical indicators (ema_50, ema_200 if available, atr_14).

        `df` must be sorted chronologically.  Only the most recent rows
        are examined.
        """
        if df.empty:
            return RegimeType.MEAN_REVERTING

        vix = self._get_vix()
        if vix is not None and vix >= _VIX_HIGH_THRESHOLD:
            return RegimeType.HIGH_VOLATILITY

        adx = self._compute_adx(df)
        if adx is not None and adx >= _ADX_TRENDING:
            return RegimeType.TRENDING

        return RegimeType.MEAN_REVERTING

    # ── VIX ───────────────────────────────────────────────────────────────────

    def _get_vix(self) -> float | None:
        """
        Return the latest VIX close.

        Priority:
          1. SQLite cache if younger than _VIX_CACHE_TTL_HOURS (4 h) — always used.
          2. Live yfinance fetch — only attempted outside a Streamlit session.
             Inside Streamlit, a stale cache is used as-is with a warning rather
             than blocking the UI thread.  Refresh by running run_pipeline.py.
        """
        cached = None
        cache_age: timedelta | None = None
        try:
            cached = get_bars(_VIX_SYMBOL, "1d", limit=1)
            if not cached.empty:
                last_ts = cached.index[-1]
                cache_age = datetime.now(timezone.utc).replace(tzinfo=None) - last_ts
                if cache_age < timedelta(hours=_VIX_CACHE_TTL_HOURS):
                    return float(cached["Close"].iloc[-1])
        except Exception:
            pass

        # Cache is stale (or absent).  Detect whether we are inside Streamlit.
        in_streamlit = False
        try:
            from streamlit.runtime import exists as _st_exists
            in_streamlit = _st_exists()
        except Exception:
            pass

        if in_streamlit:
            # Do not block the UI thread with a network call.
            # Use the stale cached value and warn once.
            if cached is not None and not cached.empty:
                age_h = cache_age.total_seconds() / 3600 if cache_age else float("inf")
                log.warning(
                    "VIX cache is %.1f h old (TTL=%d h) - using stale value inside "
                    "Streamlit to avoid blocking.  Run run_pipeline.py to refresh.",
                    age_h, _VIX_CACHE_TTL_HOURS,
                )
                return float(cached["Close"].iloc[-1])
            log.warning(
                "No cached VIX data found - regime detection will skip VIX check. "
                "Run run_pipeline.py to populate the VIX cache."
            )
            return None

        return self._fetch_vix()

    def _fetch_vix(self) -> float | None:
        try:
            hist = yf.Ticker(_VIX_SYMBOL).history(period="5d")
            if hist.empty:
                return None
            # Normalise and cache
            hist.index = hist.index.tz_localize(None) if hist.index.tzinfo is None else hist.index.tz_convert("UTC").tz_localize(None)
            upsert_bars(hist, _VIX_SYMBOL, "1d")
            return float(hist["Close"].iloc[-1])
        except Exception as exc:
            log.debug("VIX fetch failed: %s", exc)
            return None

    # ── ADX (pure-pandas, no ta dependency) ───────────────────────────────────

    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> float | None:
        """Return the most recent ADX value, or None if not enough data.

        Uses Wilder's smoothing (alpha=1/period, equivalent to span=2*period-1)
        as defined in the original 1978 ADX specification.  The earlier
        ``ewm(span=period, ...)`` here gave alpha=2/(period+1) ~ 2x faster than
        Wilder's smoothing, which made ADX too reactive and biased the
        TRENDING-regime threshold downward — visible across the universe as
        more frequent TRENDING classifications than a textbook ADX would
        produce.  Comparison at period=14: Wilder alpha=0.071 vs old code
        alpha=0.133.
        """
        needed = ["High", "Low", "Close"]
        if not all(c in df.columns for c in needed) or len(df) < period * 2:
            return None

        high  = df["High"]
        low   = df["Low"]
        close = df["Close"]

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low  - close.shift(1)).abs(),
        ], axis=1).max(axis=1)

        dm_plus  = (high - high.shift(1)).clip(lower=0)
        dm_minus = (low.shift(1) - low).clip(lower=0)
        dm_plus  = dm_plus.where(dm_plus > dm_minus, 0)
        dm_minus = dm_minus.where(dm_minus > dm_plus, 0)

        alpha = 1.0 / period
        atr   = tr.ewm(alpha=alpha, adjust=False).mean()
        di_p  = 100 * dm_plus.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, float("nan"))
        di_m  = 100 * dm_minus.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, float("nan"))
        dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, float("nan"))
        adx   = dx.ewm(alpha=alpha, adjust=False).mean()

        val = adx.iloc[-1]
        return None if pd.isna(val) else float(val)
