"""
Page 6 — Data Status

Shows one row per known symbol (static watchlist + active universe candidates)
so you can see at a glance what data is available before running the pipeline
or training models.  Symbols with no data yet appear with empty bar/news columns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from data.ui_queries import query_data_status

st.set_page_config(page_title="Data Status", layout="wide")
st.title("Data Status")
st.caption(
    "One row per known symbol — static watchlist + active universe candidates.  "
    "Symbols with no bar data yet appear with empty columns.  "
    "Run `python run_pipeline.py` to fetch OHLCV data, "
    "`python train_models.py` to build model checkpoints."
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Controls")
    if st.button("Refresh", use_container_width=True):
        query_data_status.clear()
        st.rerun()

    st.caption("Table auto-refreshes every 60 seconds.")

# ── Load data ─────────────────────────────────────────────────────────────────

df = query_data_status()

if df.empty:
    st.info(
        "No symbols found in the database.  "
        "Run `python run_pipeline.py` to seed it."
    )
    st.stop()

# ── Summary metric cards ──────────────────────────────────────────────────────

now = datetime.now(timezone.utc).replace(tzinfo=None)
stale_threshold = timedelta(days=1)

n_total      = len(df)
n_with_daily = int((df["daily_bars"] > 0).sum())
n_with_news  = int((df["news_total"] > 0).sum())

# Count symbols whose latest daily bar is more than 1 day old
n_stale = int(
    df["latest_daily"]
    .dropna()
    .apply(lambda t: (now - t) > stale_threshold)
    .sum()
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Symbols", n_total)
c2.metric("With Daily Bars", n_with_daily)
c3.metric("With News", n_with_news)
c4.metric("Stale (>1 day)", n_stale, delta=None,
          delta_color="inverse" if n_stale > 0 else "off")

st.markdown("---")

# ── Build display table ───────────────────────────────────────────────────────

def _age_str(ts) -> str:
    """Human-readable age string for a timestamp."""
    if pd.isna(ts):
        return ""
    delta = now - pd.Timestamp(ts)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)}m ago"
    if hours < 24:
        return f"{hours:.0f}h ago"
    return f"{delta.days}d ago"


display = pd.DataFrame()
display["Symbol"]        = df["symbol"]
display["Daily Bars"]    = df["daily_bars"].replace(0, pd.NA)
display["Latest Daily"]  = df["latest_daily"].dt.strftime("%Y-%m-%d").where(
    df["latest_daily"].notna(), ""
)
display["Age"]           = df["latest_daily"].apply(_age_str)
display["Hourly Bars"]   = df["hourly_bars"].replace(0, pd.NA)
display["Latest Hourly"] = df["latest_hourly"].dt.strftime("%Y-%m-%d %H:%M").where(
    df["latest_hourly"].notna(), ""
)
display["News"]          = df["news_total"].replace(0, pd.NA)
display["News Scored"]   = df["news_scored"].replace(0, pd.NA)
display["News Source"]   = df["news_source"]

def _source_breakdown(row) -> str:
    """e.g. 'IBKR 212 · yf 10' — only non-zero sources shown."""
    if row["news_total"] == 0:
        return ""
    yf = row["news_total"] - row["ibkr_count"] - row["alpaca_count"]
    parts = []
    if row["ibkr_count"] > 0:
        parts.append(f"IBKR {row['ibkr_count']}")
    if row["alpaca_count"] > 0:
        parts.append(f"Alpaca {row['alpaca_count']}")
    if yf > 0:
        parts.append(f"yf {yf}")
    return " · ".join(parts)

display["Source Counts"] = df.apply(_source_breakdown, axis=1)
display["Fundamentals"]  = df["has_fundamentals"].map({True: "✓", False: ""})

# ── Row colouring ─────────────────────────────────────────────────────────────

def _row_style(row: pd.Series) -> list[str]:
    latest = df.loc[row.name, "latest_daily"]
    if pd.isna(latest):
        # No daily bars at all
        base = "background-color: #2a1f1f"   # dark red tint
    elif (now - pd.Timestamp(latest)) > stale_threshold:
        # Has bars but stale
        base = "background-color: #2a2510"   # dark amber tint
    else:
        base = ""
    return [base] * len(row)


styled = display.style.apply(_row_style, axis=1)

st.dataframe(styled, use_container_width=True, hide_index=True, height=600)

# ── Legend ────────────────────────────────────────────────────────────────────

st.markdown("---")
col_l, col_r = st.columns(2)

with col_l:
    st.markdown(
        """
**Row colours**
- Normal — data is fresh (latest bar within 1 day)
- Amber tint — latest daily bar is more than 1 day old
- Red tint — no daily bars in the database at all
        """
    )

with col_r:
    st.markdown(
        """
**Column guide**
- **Daily / Hourly Bars** — total bars stored per interval
- **Latest Daily / Age** — timestamp of the most recent 1d bar
- **News / News Scored** — articles cached; scored = FinBERT sentiment assigned
- **News Source** — dominant source: IBKR / Alpaca / yfinance; `+` suffix means mixed sources
- **Source Counts** — per-source article breakdown, e.g. `IBKR 212 · yf 10`
- **Fundamentals** — yfinance fundamentals snapshot exists (24h cache)
        """
    )
