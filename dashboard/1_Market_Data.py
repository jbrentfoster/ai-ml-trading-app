"""
AI Trading Dashboard — Market Data & Indicators (Page 1)
=========================================================
Candlestick chart with overlay toggles, RSI, MACD, ATR panels,
volume bar chart, and a full OHLCV + indicator data table with CSV export.

Run with:
    streamlit run dashboard/1_Market_Data.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from config.settings import config
from data.fetcher import DataFetcher
from data.indicators import IndicatorEngine, compute_indicators
from data.ui_queries import query_bars, query_company_name, query_watchlist_summary, symbol_picker

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Market Data",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("📈 AI Trading App")
st.sidebar.caption(f"Mode: **{config.trading.mode.value.upper()}**")
st.sidebar.markdown("---")

watchlist: list[str] = config.data.watchlist
symbol: str   = symbol_picker("Symbol", default="AAPL", key="md_symbol")
interval: str = st.sidebar.selectbox("Interval", ["1d", "1h"], index=0, key="md_interval")

st.sidebar.markdown("**Date range**")
_today  = datetime.now(timezone.utc).date()
_default_start = _today - timedelta(days=365)
date_start = st.sidebar.date_input("From", value=_default_start, key="md_start")
date_end   = st.sidebar.date_input("To",   value=_today,         key="md_end")

st.sidebar.markdown("**Overlays**")
show_bb   = st.sidebar.checkbox("Bollinger Bands",  value=True,  key="md_bb")
show_ema  = st.sidebar.checkbox("EMA 9 / 21 / 50",  value=True,  key="md_ema")
show_ma50 = st.sidebar.checkbox("SMA 50",           value=False, key="md_ma50")
show_ma200= st.sidebar.checkbox("SMA 200",          value=False, key="md_ma200")

st.sidebar.markdown("---")

col_r1, col_r2 = st.sidebar.columns(2)
with col_r1:
    if st.button("Refresh", key="md_refresh_sym", use_container_width=True):
        with st.spinner(f"Fetching {symbol} …"):
            DataFetcher().fetch_symbol(symbol, interval=interval)
            IndicatorEngine().run(symbol, interval=interval)
        st.rerun()
with col_r2:
    if st.button("All", key="md_refresh_all", use_container_width=True):
        with st.spinner(f"Refreshing {len(watchlist)} symbols …"):
            fetcher = DataFetcher()
            engine  = IndicatorEngine()
            for sym in watchlist:
                fetcher.fetch_symbol(sym, interval=interval)
                engine.run(sym, interval=interval)
        st.rerun()

# ── Load data ─────────────────────────────────────────────────────────────────

# Fetch full 1000-bar history so SMA 200 can be computed correctly,
# then filter to the selected date range for display.
df_full = query_bars(symbol, interval, limit=1000)

if df_full.empty:
    st.warning(
        f"No data for **{symbol}** ({interval}).  "
        "Click **Refresh** in the sidebar to download it."
    )
    st.stop()

df_full = compute_indicators(df_full)

# Compute SMAs on the full dataset (needs 200 bars of history)
df_full["sma_50"]  = df_full["Close"].rolling(50).mean()
df_full["sma_200"] = df_full["Close"].rolling(200).mean()

# Filter to selected date range for display
df = df_full.copy()
if date_start:
    df = df[df.index >= pd.Timestamp(date_start)]
if date_end:
    df = df[df.index <= pd.Timestamp(date_end)]

if df.empty:
    st.warning("No bars in the selected date range.  Adjust the date picker.")
    st.stop()

# ── Header metrics ────────────────────────────────────────────────────────────

_company = query_company_name(symbol)
_header  = f"## {symbol}" + (f" — {_company}" if _company else "") + f" &nbsp; `{interval}`"
st.markdown(_header)

latest = df.iloc[-1]
prev   = df.iloc[-2] if len(df) > 1 else latest


def _fmt(val, fmt=".2f") -> str:
    return f"{val:{fmt}}" if pd.notna(val) else "—"


price_delta = latest["Close"] - prev["Close"]
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Close",    f"${latest['Close']:.2f}",  f"{price_delta:+.2f}")
c2.metric("High",     f"${latest['High']:.2f}")
c3.metric("Low",      f"${latest['Low']:.2f}")
c4.metric("RSI (14)", _fmt(latest.get("rsi_14"), ".1f"))
c5.metric("MACD",     _fmt(latest.get("macd"), ".3f"))
c6.metric("ATR (14)", f"${_fmt(latest.get('atr_14'))}")

st.markdown("---")

st.caption(
    "**How to read this chart —** "
    "Candlesticks show Open / High / Low / Close for each bar (green = closed higher, red = closed lower).  "
    "**Bollinger Bands** (2σ around a 20-bar SMA) widen during high volatility and narrow in quiet markets; "
    "prices touching the outer band often precede a mean-reversion.  "
    "**EMAs** (9 / 21 / 50) track short, medium, and longer-term momentum — a shorter EMA crossing above a longer one "
    "signals building upward momentum.  "
    "**RSI** > 70 = overbought, < 30 = oversold.  "
    "**MACD** histogram growing green = accelerating bullish momentum; growing red = accelerating bearish momentum.  "
    "**ATR** is a volatility gauge with no directional bias — the signal gate raises buy/sell thresholds when ATR is elevated."
)

# ── Main chart (4 subplots: Price | RSI | MACD | ATR) ─────────────────────────

fig = make_subplots(
    rows=4, cols=1,
    shared_xaxes=True,
    row_heights=[0.50, 0.17, 0.17, 0.16],
    vertical_spacing=0.025,
    subplot_titles=(f"{symbol} Price", "RSI (14)", "MACD", "ATR (14)"),
)

# — Candlesticks —
fig.add_trace(go.Candlestick(
    x=df.index,
    open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"],
    name="OHLC",
    increasing_line_color="#26a69a",
    decreasing_line_color="#ef5350",
    showlegend=False,
), row=1, col=1)

# — Bollinger Bands —
if show_bb and "bb_upper" in df.columns:
    _bc = "rgba(100,181,246,0.55)"
    _fc = "rgba(100,181,246,0.07)"
    fig.add_trace(go.Scatter(x=df.index, y=df["bb_upper"], name="BB Upper",
        line=dict(color=_bc, width=1)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["bb_middle"], name="BB Mid",
        line=dict(color=_bc, width=1, dash="dot"), showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["bb_lower"], name="BB Lower",
        line=dict(color=_bc, width=1),
        fill="tonexty", fillcolor=_fc, showlegend=False), row=1, col=1)

# — EMAs —
if show_ema:
    for col, color, name in [
        ("ema_9",  "#ff9800", "EMA 9"),
        ("ema_21", "#2196f3", "EMA 21"),
        ("ema_50", "#9c27b0", "EMA 50"),
    ]:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df[col], name=name,
                line=dict(color=color, width=1.4)), row=1, col=1)

# — SMA overlays —
if show_ma50 and "sma_50" in df.columns:
    fig.add_trace(go.Scatter(x=df.index, y=df["sma_50"], name="SMA 50",
        line=dict(color="#00bcd4", width=1.5, dash="dash")), row=1, col=1)
if show_ma200 and "sma_200" in df.columns:
    fig.add_trace(go.Scatter(x=df.index, y=df["sma_200"], name="SMA 200",
        line=dict(color="#ff5722", width=1.5, dash="dash")), row=1, col=1)

# — RSI —
if "rsi_14" in df.columns:
    fig.add_trace(go.Scatter(x=df.index, y=df["rsi_14"], name="RSI",
        line=dict(color="#ff6d00", width=1.5), showlegend=False), row=2, col=1)
    for level, clr in [(70, "rgba(239,83,80,0.6)"), (30, "rgba(38,166,154,0.6)")]:
        fig.add_hline(y=level, line_dash="dash", line_color=clr, row=2, col=1)

# — MACD —
if "macd" in df.columns:
    hist_colors = ["#26a69a" if v >= 0 else "#ef5350"
                   for v in df["macd_hist"].fillna(0)]
    fig.add_trace(go.Bar(x=df.index, y=df["macd_hist"], name="Histogram",
        marker_color=hist_colors, showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["macd"], name="MACD",
        line=dict(color="#2196f3", width=1.5)), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["macd_signal"], name="Signal",
        line=dict(color="#ff9800", width=1.5)), row=3, col=1)

# — ATR —
if "atr_14" in df.columns:
    fig.add_trace(go.Scatter(x=df.index, y=df["atr_14"], name="ATR",
        line=dict(color="#ab47bc", width=1.5), fill="tozeroy",
        fillcolor="rgba(171,71,188,0.1)", showlegend=False), row=4, col=1)

fig.update_layout(
    height=820,
    template="plotly_dark",
    xaxis_rangeslider_visible=False,
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
    margin=dict(l=0, r=0, t=40, b=0),
)
fig.update_yaxes(title_text="Price ($)", row=1, col=1)
fig.update_yaxes(title_text="RSI",       row=2, col=1, range=[0, 100])
fig.update_yaxes(title_text="MACD",      row=3, col=1)
fig.update_yaxes(title_text="ATR ($)",   row=4, col=1)

st.plotly_chart(fig, use_container_width=True)

# ── Volume chart ──────────────────────────────────────────────────────────────

vol_colors = [
    "#26a69a" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "#ef5350"
    for i in range(len(df))
]
vol_fig = go.Figure()
vol_fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="Volume",
    marker_color=vol_colors, showlegend=False))
if "volume_sma_20" in df.columns:
    vol_fig.add_trace(go.Scatter(x=df.index, y=df["volume_sma_20"], name="Vol SMA (20)",
        line=dict(color="#ff9800", width=1.5)))
vol_fig.update_layout(
    height=140, template="plotly_dark",
    margin=dict(l=0, r=0, t=0, b=0),
    xaxis_rangeslider_visible=False,
    legend=dict(orientation="h", yanchor="top", y=1, xanchor="right", x=1),
)
st.plotly_chart(vol_fig, use_container_width=True)
st.caption(
    "Volume bars are colored to match price direction (green = up bar, red = down bar).  "
    "The orange line is the 20-bar Volume SMA.  "
    "A price breakout accompanied by above-average volume is considered more reliable — "
    "thin-volume moves are more likely to reverse.  Volume SMA is one of the XGBoost input features."
)

# ── Data table with CSV export ────────────────────────────────────────────────

st.markdown("---")
st.subheader("OHLCV + Indicators")

display_cols = [
    "Open", "High", "Low", "Close", "Volume",
    "rsi_14", "macd", "macd_signal", "bb_upper", "bb_lower",
    "ema_9", "ema_21", "ema_50", "sma_50", "sma_200",
    "atr_14", "volume_sma_20",
]
table_df = df[[c for c in display_cols if c in df.columns]].copy()
table_df.index.name = "Date"
table_df = table_df.sort_index(ascending=False)

st.dataframe(
    table_df.style.format({c: "{:.4f}" for c in table_df.columns}, na_rep="—"),
    use_container_width=True,
    height=320,
)

csv_bytes = table_df.reset_index().to_csv(index=False).encode("utf-8")
st.download_button(
    label="Download CSV",
    data=csv_bytes,
    file_name=f"{symbol}_{interval}_indicators.csv",
    mime="text/csv",
)

# ── Watchlist overview ────────────────────────────────────────────────────────

st.markdown("---")
with st.expander("Watchlist overview", expanded=False):
    summary_df = query_watchlist_summary(tuple(watchlist), interval)

    def _clr_chg(val):
        if val is None or pd.isna(val):
            return ""
        return "color: #26a69a" if val >= 0 else "color: #ef5350"

    fmt = {
        "Close":   "${:.2f}",
        "Change%": "{:+.2f}%",
        "RSI":     "{:.1f}",
        "MACD":    "{:.3f}",
        "ATR":     "${:.2f}",
    }
    st.dataframe(
        summary_df.style
            .format(fmt, na_rep="—")
            .map(_clr_chg, subset=["Change%"]),
        use_container_width=True,
    )

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    f"Data via Yahoo Finance · "
    f"Mode: {config.trading.mode.value.upper()} · "
    f"Last render: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
)
