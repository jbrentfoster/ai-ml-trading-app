"""
Fundamental data client — yfinance with SQLite caching.

Fetches a curated set of fundamental metrics from yfinance and caches them
in the fundamental_data table.  The cache is treated as stale after 24 hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yfinance as yf

from config.settings import config
from core.logger import get_logger
from data.database import get_fundamentals, upsert_fundamentals

log = get_logger("data.fundamentals")

# How old a cached snapshot can be before we re-fetch from yfinance
_CACHE_TTL_HOURS = 24


def _safe_float(value, fallback: float | None = None) -> float | None:
    """Convert a potentially missing/NaN/inf value to float or fallback.

    Why: yfinance returns inf for undefined ratios (e.g. forward P/E when
    forward earnings are zero — POET 2026-05-11 crashed XGBoost training
    with `Input data contains 'inf' or a value too large`).
    """
    try:
        v = float(value)
        import math
        return fallback if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return fallback


class FundamentalsClient:
    """Fetch and cache yfinance fundamental snapshots."""

    def get(self, symbol: str, force_refresh: bool = False) -> dict:
        """
        Return fundamental data for `symbol`.

        Reads from SQLite cache if the snapshot is < 24 hours old.
        Falls back to zero/None values if yfinance is unavailable.

        Returns a flat dict with keys matching FundamentalData columns.
        """
        if not force_refresh:
            cached = get_fundamentals(symbol)
            if cached and cached.get("fetched_at"):
                age = datetime.now(timezone.utc).replace(tzinfo=None) - cached["fetched_at"]
                if age < timedelta(hours=_CACHE_TTL_HOURS):
                    log.debug("Using cached fundamentals for %s (age=%s)", symbol, age)
                    return cached

        return self._fetch_and_cache(symbol)

    def _fetch_and_cache(self, symbol: str) -> dict:
        try:
            info = yf.Ticker(symbol).info
        except Exception as exc:
            log.warning("yfinance fundamentals failed for %s: %s", symbol, exc)
            info = {}

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        data = {
            "fetched_at":      now,
            "market_cap":      _safe_float(info.get("marketCap")),
            "pe_ratio":        _safe_float(info.get("trailingPE")),
            "forward_pe":      _safe_float(info.get("forwardPE")),
            "price_to_book":   _safe_float(info.get("priceToBook")),
            "ev_to_ebitda":    _safe_float(info.get("enterpriseToEbitda")),
            "revenue_growth":  _safe_float(info.get("revenueGrowth")),
            "earnings_growth": _safe_float(info.get("earningsGrowth")),
            "profit_margin":   _safe_float(info.get("profitMargins")),
            "roe":             _safe_float(info.get("returnOnEquity")),
            "debt_to_equity":  _safe_float(info.get("debtToEquity")),
            "current_ratio":   _safe_float(info.get("currentRatio")),
            "free_cashflow":   _safe_float(info.get("freeCashflow")),
            "analyst_target":  _safe_float(info.get("targetMeanPrice")),
        }

        upsert_fundamentals(symbol, data)
        log.debug("Fetched and cached fundamentals for %s", symbol)
        return {"symbol": symbol, **data}

    def get_feature_vector(self, symbol: str) -> dict[str, float]:
        """
        Return a normalised feature dict suitable for XGBoost input.
        Missing / non-finite values are filled with 0.0.
        """
        import math
        raw = self.get(symbol)
        feature_keys = [
            "market_cap", "pe_ratio", "forward_pe", "price_to_book",
            "ev_to_ebitda", "revenue_growth", "earnings_growth",
            "profit_margin", "roe", "debt_to_equity", "current_ratio",
            "free_cashflow", "analyst_target",
        ]
        out: dict[str, float] = {}
        for k in feature_keys:
            v = raw.get(k)
            try:
                f = float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                f = 0.0
            out[k] = 0.0 if (math.isnan(f) or math.isinf(f)) else f
        return out
