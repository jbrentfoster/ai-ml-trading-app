"""
Fundamentals & News — Page 2
==============================
Key fundamental metrics sourced from the SQLite fundamental_data table,
plus cached FinBERT-scored news headlines from the news_cache table.

No direct API calls are made from this page.
"""

from __future__ import annotations

from pathlib import Path
import sys
from datetime import datetime, timezone

_root = Path(__file__).resolve()
while not (_root / "config" / "settings.py").exists() and _root.parent != _root:
    _root = _root.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config.settings import config
from data.ui_queries import query_company_name, query_fundamentals, query_news, symbol_picker

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Fundamentals & News",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("📊 Fundamentals & News")
st.sidebar.markdown("---")

symbol = symbol_picker("Symbol", default="AAPL", key="fn_symbol")
news_days = st.sidebar.slider("News lookback (days)", 7, 90, 30, key="fn_days")

st.sidebar.markdown("**Fetch News**")
score_news = st.sidebar.checkbox(
    "Score with FinBERT",
    value=True,
    key="fn_score",
    help="Run ProsusAI/finbert on each headline (slow on first load — model downloads ~400 MB)",
)
fetch_btn = st.sidebar.button("Fetch & Score News", type="primary", key="fn_fetch")

st.sidebar.markdown("---")
if st.sidebar.button("Refresh cache", key="fn_refresh"):
    query_fundamentals.clear()
    query_news.clear()
    st.rerun()

# ── Fetch news (runs before display so results are visible immediately) ────────

if fetch_btn:
    with st.spinner(f"Fetching news for {symbol} …"):
        try:
            from data.news_client import NewsClient
            from data.database import get_recent_news, upsert_news
            from datetime import timedelta, timezone

            client   = NewsClient()
            articles = client.fetch_news(symbol, days_back=news_days, force_refresh=True)

            if not articles:
                st.warning("No news articles found for the selected symbol and lookback window.")
            else:
                scored = 0
                if score_news:
                    from models.finbert_model import FinBERTModel
                    finbert = FinBERTModel()
                    pipe    = finbert._get_pipeline()

                    if pipe is None:
                        st.warning(
                            "FinBERT pipeline could not be loaded — "
                            "articles stored without sentiment scores."
                        )
                    else:
                        for art in articles:
                            if art.get("sentiment_score") is not None:
                                continue
                            score = finbert._score_headline(art["headline"])
                            upsert_news(
                                symbol=symbol,
                                article_id=art["article_id"],
                                published_at=art["published_at"],
                                headline=art["headline"],
                                sentiment_score=score,
                            )
                            scored += 1

                query_news.clear()
                st.success(
                    f"Fetched {len(articles)} article(s) for {symbol}.  "
                    + (f"FinBERT scored {scored} new article(s)." if score_news else "")
                )
                st.rerun()
        except Exception as exc:
            st.error(f"News fetch failed: {exc}")

# ── Main ──────────────────────────────────────────────────────────────────────

_company = query_company_name(symbol)
st.title(f"Fundamentals & News — {symbol}" + (f" ({_company})" if _company else ""))
st.caption(
    "Data sourced from the local SQLite cache.  "
    "Run `python run_pipeline.py` or use the **Market Data** page refresh "
    "to update fundamentals and news."
)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — FUNDAMENTALS
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Fundamental Metrics")

fund = query_fundamentals(symbol)

if not fund or fund.get("fetched_at") is None:
    st.info(
        f"No fundamental data cached for **{symbol}**.  "
        "Fundamentals are fetched automatically when signals are generated.  "
        "You can also call `data.fundamentals.FundamentalsClient().get('{}')` "
        "from a Python script.".format(symbol)
    )
else:
    # ── Fetched-at caption ──────────────────────────────────────────────────
    fetched = fund.get("fetched_at")
    if isinstance(fetched, datetime):
        age_h = (datetime.now(timezone.utc).replace(tzinfo=None) - fetched).total_seconds() / 3600
        local_tz  = datetime.now(timezone.utc).astimezone().tzinfo
        fetched_local = fetched.replace(tzinfo=timezone.utc).astimezone(local_tz)
        st.caption(
            f"Last refreshed: {fetched_local.strftime('%Y-%m-%d %H:%M %Z')}  "
            f"({age_h:.0f}h ago)"
        )

    # ── Key metric cards ────────────────────────────────────────────────────
    def _card(col, label: str, value, fmt: str = "{:.2f}", suffix: str = "") -> None:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            col.metric(label, "—")
        else:
            col.metric(label, f"{fmt.format(value)}{suffix}")

    mc = fund.get("market_cap")
    mc_str = (
        f"${mc/1e12:.2f}T" if mc and mc >= 1e12
        else f"${mc/1e9:.1f}B" if mc and mc >= 1e9
        else "—"
    )

    r1c1, r1c2, r1c3 = st.columns(3)
    r2c1, r2c2, r2c3 = st.columns(3)

    r1c1.metric("Market Cap",       mc_str)
    _card(r1c2, "P/E Ratio",        fund.get("pe_ratio"),        "{:.1f}x")
    _card(r1c3, "Forward P/E",      fund.get("forward_pe"),      "{:.1f}x")
    _card(r2c1, "Price / Book",     fund.get("price_to_book"),   "{:.2f}x")
    _card(r2c2, "Revenue Growth",   fund.get("revenue_growth"),  "{:+.1%}")
    _card(r2c3, "Profit Margin",    fund.get("profit_margin"),   "{:.1%}")

    r3c1, r3c2, r3c3 = st.columns(3)
    _card(r3c1, "Debt / Equity",    fund.get("debt_to_equity"),  "{:.2f}")
    _card(r3c2, "Current Ratio",    fund.get("current_ratio"),   "{:.2f}")
    _card(r3c3, "Return on Equity", fund.get("roe"),             "{:.1%}")

    # ── Metrics bar chart ────────────────────────────────────────────────────
    st.markdown("#### Key Ratios")
    st.caption(
        "Valuation ratios compare price to company fundamentals.  "
        "**P/E** (trailing) and **Forward P/E** measure how much investors pay per dollar of current and expected earnings — "
        "lower values suggest cheaper valuation relative to sector peers.  "
        "**Price/Book** compares market cap to net asset value; values below 1.0 mean the stock trades below book.  "
        "**EV/EBITDA** accounts for debt in the price (enterprise value vs. cash earnings) — useful for comparing "
        "companies with different capital structures.  "
        "**Debt/Equity** and **Current Ratio** gauge balance-sheet health — high debt or a current ratio below 1.0 "
        "can signal liquidity risk.  The XGBoost model uses these as input features alongside price indicators."
    )

    ratio_data = {
        "P/E Ratio":      fund.get("pe_ratio"),
        "Forward P/E":    fund.get("forward_pe"),
        "Price/Book":     fund.get("price_to_book"),
        "EV/EBITDA":      fund.get("ev_to_ebitda"),
        "Debt/Equity":    fund.get("debt_to_equity"),
        "Current Ratio":  fund.get("current_ratio"),
    }
    ratio_df = pd.DataFrame([
        {"Metric": k, "Value": v}
        for k, v in ratio_data.items()
        if v is not None and not pd.isna(v)
    ])

    if not ratio_df.empty:
        ratio_fig = px.bar(
            ratio_df, x="Metric", y="Value",
            color_discrete_sequence=["#2196f3"],
            template="plotly_dark",
        )
        ratio_fig.update_layout(height=280, margin=dict(l=0, r=0, t=20, b=0),
                                 showlegend=False)
        st.plotly_chart(ratio_fig, use_container_width=True)
    else:
        st.info("Not enough ratio data to plot.")

    # ── Growth metrics bar chart ─────────────────────────────────────────────
    growth_data = {
        "Revenue Growth":   fund.get("revenue_growth"),
        "Earnings Growth":  fund.get("earnings_growth"),
        "Profit Margin":    fund.get("profit_margin"),
        "ROE":              fund.get("roe"),
    }
    growth_df = pd.DataFrame([
        {"Metric": k, "Value": round(v * 100, 2)}
        for k, v in growth_data.items()
        if v is not None and not pd.isna(v)
    ])

    if not growth_df.empty:
        st.markdown("#### Growth & Profitability (%)")
        st.caption(
            "Year-over-year growth and efficiency metrics expressed as percentages.  "
            "**Revenue Growth** and **Earnings Growth** show how fast the business is expanding.  "
            "**Profit Margin** (net income / revenue) reveals how much of each sales dollar reaches the bottom line — "
            "tech companies often run 20–30%+, while retailers may be 2–5%.  "
            "**ROE** (Return on Equity) measures how efficiently management turns shareholder capital into profit; "
            "values above 15% are generally considered strong.  Green = positive, red = negative."
        )
        colors = ["#26a69a" if v >= 0 else "#ef5350" for v in growth_df["Value"]]
        g_fig = go.Figure(go.Bar(
            x=growth_df["Metric"], y=growth_df["Value"],
            marker_color=colors,
        ))
        g_fig.update_layout(height=260, template="plotly_dark",
                            margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(g_fig, use_container_width=True)

    # ── Full fundamentals table ───────────────────────────────────────────────
    with st.expander("All cached fundamental data"):
        excl = {"id", "symbol", "fetched_at"}
        rows = [
            {"Field": k.replace("_", " ").title(), "Value": v}
            for k, v in fund.items()
            if k not in excl and v is not None
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — NEWS & SENTIMENT
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("News & Sentiment")

news_df = query_news(symbol, days_back=news_days)

if news_df.empty:
    st.info(
        f"No cached news for **{symbol}** in the last {news_days} days.  "
        "Click **Fetch & Score News** in the sidebar to pull headlines via "
        "yfinance (no API key required) or Alpaca Markets if keys are configured."
    )
else:
    # ── Sentiment summary cards ───────────────────────────────────────────────
    scored = news_df.dropna(subset=["sentiment_score"])
    n_pos  = (news_df["sentiment_label"] == "Positive").sum()
    n_neg  = (news_df["sentiment_label"] == "Negative").sum()
    n_neu  = (news_df["sentiment_label"] == "Neutral").sum()
    avg_s  = scored["sentiment_score"].mean() if not scored.empty else 0.0

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Total Articles", len(news_df))
    sc2.metric("Positive",       int(n_pos), delta=None)
    sc3.metric("Negative",       int(n_neg), delta=None)
    sc4.metric("Avg Score",      f"{avg_s:+.3f}")

    # ── Rolling 7-day sentiment trend ────────────────────────────────────────
    if not scored.empty:
        trend = (
            scored
            .set_index("published_at")["sentiment_score"]
            .sort_index()
            .rolling("7D", min_periods=1)
            .mean()
            .reset_index()
        )
        trend.columns = ["Date", "7-Day Avg Sentiment"]

        trend_fig = go.Figure()
        trend_fig.add_trace(go.Scatter(
            x=trend["Date"], y=trend["7-Day Avg Sentiment"],
            name="7-day rolling avg",
            line=dict(color="#2196f3", width=2),
            fill="tozeroy",
            fillcolor="rgba(33,150,243,0.1)",
        ))
        trend_fig.add_hline(y=0, line_dash="dash",
                            line_color="rgba(255,255,255,0.3)")
        trend_fig.update_layout(
            height=240, template="plotly_dark",
            title="Rolling 7-Day Average Sentiment Score",
            yaxis=dict(title="Score", range=[-1, 1]),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(trend_fig, use_container_width=True)
        st.caption(
            "Each headline is scored by **ProsusAI/FinBERT**, a BERT-based language model fine-tuned on financial news.  "
            "Scores run from -1 (strongly negative) to +1 (strongly positive); 0 = neutral.  "
            "The 7-day rolling average smooths out one-off headlines to reveal the underlying sentiment trend.  "
            "This aggregated score feeds directly into the ensemble model as the FinBERT component, "
            "weighted alongside LSTM and XGBoost.  An exponential time-decay is applied so older articles "
            "contribute less than recent ones."
        )

    # ── News table with colour coding ─────────────────────────────────────────
    st.markdown("#### Recent Headlines")

    def _row_color(row) -> list[str]:
        label = row.get("sentiment_label", "Neutral")
        if label == "Positive":
            return ["background-color: rgba(38,166,154,0.15)"] * len(row)
        if label == "Negative":
            return ["background-color: rgba(239,83,80,0.15)"] * len(row)
        return ["background-color: rgba(150,150,150,0.05)"] * len(row)

    display_cols = ["published_at", "headline", "sentiment_score", "sentiment_label"]
    display_cols = [c for c in display_cols if c in news_df.columns]

    table_news = news_df[display_cols].rename(columns={
        "published_at":    "Date",
        "headline":        "Headline",
        "sentiment_score": "Score",
        "sentiment_label": "Sentiment",
    })

    st.dataframe(
        table_news.style
            .apply(_row_color, axis=1)
            .format({"Score": "{:+.3f}"}, na_rep="—"),
        use_container_width=True,
        height=400,
    )

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    "Fundamentals via Yahoo Finance · News via Alpaca Markets · "
    "Sentiment scored by ProsusAI/FinBERT"
)
