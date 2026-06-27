"""Security sector classification (kept utility).

Extracted from the retired risk/portfolio_guard.py in the 2026-06 restructure
(see archive/README.md).  Two-tier lookup: the hardcoded _SECTOR_MAP (ETFs /
fixtures / hand-pins) then the yfinance GICS sector stored in fundamental_data,
normalised into the project buckets.  Used by the dashboard Account page, the
sector backfill, and the value+quality screen.
"""

from __future__ import annotations


# This is the *first* lookup tier in get_sector(); individual equities not
# listed here fall back to the yfinance sector captured in fundamental_data
# (see get_sector below).  ETFs are kept here because yfinance does not assign
# them a usable GICS sector — a sector ETF (XLF, XLK, …) must map to the sector
# it *tracks*, and broad-market / bond / commodity ETFs have no single sector.
#
# Sector buckets are simplified from S&P GICS — "Technology" = Information
# Technology; "Telecom" = Communication Services; "Consumer Disc" =
# Consumer Discretionary; "Financials" = Financial Services; "Materials" =
# Basic Materials.  The yfinance-fallback path normalises raw GICS labels into
# these same buckets via _YF_SECTOR_NORMALIZE so both tiers agree.
_SECTOR_MAP: dict[str, str] = {
    # ── Broad-market ETF fixtures ────────────────────────────────────────────
    "SPY": "Broad Market", "QQQ": "Broad Market",
    "IWM": "Broad Market", "DIA": "Broad Market",

    # ── Sector ETF fixtures ──────────────────────────────────────────────────
    "XLF": "Financials",   "XLE": "Energy",
    "XLK": "Technology",   "XLV": "Healthcare",
    "XLI": "Industrials",  "XLP": "Consumer Staples",
    "XLY": "Consumer Disc", "XLU": "Utilities",
    "XLB": "Materials",    "XLRE": "Real Estate",

    # ── Bond / commodity ETF fixtures ────────────────────────────────────────
    "TLT": "Fixed Income",
    "GLD": "Commodities",  "SLV": "Commodities", "USO": "Commodities",

    # ── Technology ───────────────────────────────────────────────────────────
    "AAPL": "Technology", "MSFT": "Technology",
    "GOOGL": "Technology", "GOOG": "Technology",
    "META": "Technology", "NVDA": "Technology",
    "AXTI": "Technology", "CRWV": "Technology",
    "MCHP": "Technology", "MSI": "Technology",
    "NOW": "Technology",  "POET": "Technology",
    "SNOW": "Technology", "TEL": "Technology",
    "TTD": "Technology",

    # ── Consumer Discretionary ───────────────────────────────────────────────
    "AMZN": "Consumer Disc", "TSLA": "Consumer Disc",
    "EXPE": "Consumer Disc", "LULU": "Consumer Disc",
    "NFLX": "Consumer Disc", "NKE": "Consumer Disc",
    "TSCO": "Consumer Disc",

    # ── Financials ───────────────────────────────────────────────────────────
    "JPM":  "Financials", "BAC": "Financials",
    "GS":   "Financials", "V":   "Financials",
    "MA":   "Financials", "BRK.B": "Financials",
    "AON":  "Financials", "CRCL": "Financials",
    "HUT":  "Financials", "SCHW": "Financials",
    "WFC":  "Financials",

    # ── Healthcare ───────────────────────────────────────────────────────────
    "UNH":  "Healthcare", "JNJ": "Healthcare",
    "LLY":  "Healthcare", "ABBV": "Healthcare",
    "ABT":  "Healthcare", "AZN": "Healthcare",
    "BDX":  "Healthcare", "BMY": "Healthcare",
    "BSX":  "Healthcare", "EW":  "Healthcare",
    "HCA":  "Healthcare", "MCK": "Healthcare",
    "MDT":  "Healthcare", "MRK": "Healthcare",
    "REGN": "Healthcare", "ZTS": "Healthcare",

    # ── Energy ───────────────────────────────────────────────────────────────
    "XOM":  "Energy", "CVX": "Energy",
    "BP":   "Energy", "DVN": "Energy",
    "FANG": "Energy", "OXY": "Energy",
    "VLO":  "Energy",

    # ── Consumer Staples ─────────────────────────────────────────────────────
    "WMT":  "Consumer Staples", "PG":  "Consumer Staples",
    "KO":   "Consumer Staples", "PEP": "Consumer Staples",
    "SYY":  "Consumer Staples",

    # ── Telecom / Communication Services ─────────────────────────────────────
    "T":    "Telecom", "VZ":   "Telecom",
    "ASTS": "Telecom", "CHTR": "Telecom",
    "TMUS": "Telecom",

    # ── Industrials ──────────────────────────────────────────────────────────
    "DAL":  "Industrials", "GE":   "Industrials",
    "LHX":  "Industrials", "LMT":  "Industrials",
    "MMM":  "Industrials", "NOC":  "Industrials",
    "ODFL": "Industrials", "UAL":  "Industrials",

    # ── Materials ────────────────────────────────────────────────────────────
    "AEM":  "Materials", "DOW":  "Materials",
    "LYB":  "Materials", "USAR": "Materials",

    # ── Utilities ────────────────────────────────────────────────────────────
    "ETR":  "Utilities",
}

# Public alias — dashboards and analytics consumers should import this name.
# The leading-underscore form remains the canonical one used by PortfolioGuard
# itself; keeping both lets internal call sites stay unchanged.
SECTOR_MAP: dict[str, str] = _SECTOR_MAP

# Raw yfinance GICS sector labels → the project's simplified buckets.  yfinance
# returns the long-form GICS names ("Financial Services", "Consumer Cyclical",
# …); normalising them here keeps the data-driven fallback in the same bucket
# scheme as the hardcoded _SECTOR_MAP, so the same sector never splits into two
# rows in the Account-page exposure aggregation.
_YF_SECTOR_NORMALIZE: dict[str, str] = {
    "Technology":             "Technology",
    "Healthcare":             "Healthcare",
    "Financial Services":     "Financials",
    "Consumer Cyclical":      "Consumer Disc",
    "Consumer Defensive":     "Consumer Staples",
    "Communication Services": "Telecom",
    "Industrials":            "Industrials",
    "Energy":                 "Energy",
    "Basic Materials":        "Materials",
    "Real Estate":            "Real Estate",
    "Utilities":              "Utilities",
}


def _normalize_yf_sector(raw: str | None) -> str | None:
    """Map a raw yfinance GICS sector label to the project bucket, or None."""
    if not raw:
        return None
    return _YF_SECTOR_NORMALIZE.get(raw.strip(), raw.strip())


def get_sector(symbol: str) -> str:
    """Return the project sector bucket for a symbol, or 'Unknown' when unknown.

    Two-tier lookup:
      1. The hardcoded _SECTOR_MAP — authoritative for ETFs / fixtures (which
         yfinance cannot classify) and any hand-pinned ticker.
      2. The latest yfinance GICS sector stored in fundamental_data, normalised
         into the project's bucket scheme.  This is what keeps the
         weekly-rotating dynamic universe classified without hand-maintaining
         the map — every symbol that has had fundamentals fetched (all trained /
         scored symbols) resolves automatically.

    Returns 'Unknown' only when a symbol is in neither tier (no map entry and no
    fundamentals row yet — e.g. a brand-new symbol before its first pipeline
    run).  A DB hiccup also degrades safely to 'Unknown'.
    """
    sym = symbol.upper()
    mapped = _SECTOR_MAP.get(sym)
    if mapped is not None:
        return mapped

    try:
        from data.database import get_latest_sector
        normalized = _normalize_yf_sector(get_latest_sector(sym))
    except Exception:  # pragma: no cover - DB unavailable degrades to Unknown
        normalized = None
    return normalized or "Unknown"
