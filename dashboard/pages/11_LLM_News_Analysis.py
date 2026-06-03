"""
Page 11 — LLM News Analysis (shadow workflow)

Full-article sentiment from a local 8B model (Ollama), over stage-3 universe
news.  PARALLEL / SHADOW signal — nothing in signal_runner reads it.  Surfaces
what the headline-only FinBERT path can't: article *attribution* (a story tagged
to one ticker is often about another) and a transparent, decomposable score.

Built event-centric and table-first so it scales to thousands of articles and
months of data:
  1. Events table — one row per de-duplicated event (sortable / filterable).
     The score is the MEAN of every read in the event (re-reports merged).
  2. Symbol drill-down — pick a ticker -> sentiment time series + its events +
     per-event detail (decomposition + the underlying articles).
  3. Research (collapsed) — distribution, cross-check, run telemetry.

Producers (run first): scripts/ingest_news_bodies.py -> scripts/score_news_llm.py
"""

from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config.settings import config
from data.ui_queries import (
    query_llm_news_analysis,
    query_llm_analysis_options,
    query_news_body,
)

TEAL = "#26a69a"
RED = "#ef5350"
GREY = "#787b86"

st.set_page_config(page_title="LLM News Analysis", layout="wide")
st.title("📰 LLM News Analysis — shadow workflow")
st.caption(
    "Full-article sentiment from a local 8B model. **Not used by the trading "
    "engine** — a research signal. Each *event* score is the mean of every "
    "article read about that event; re-reports of one story are merged."
)

with st.expander("ℹ️ How to read this page — columns & scoring"):
    st.markdown(
        """
**Columns**
- **Ticker** — the company the article is *about* (resolved from the text;
  blank for digests). **Feed** — the IBKR symbol tag it arrived under.
- **Attrib** — read-time attribution status:
  **✓ match** (about the feed symbol) · **↪ re-attr** (about a *different*
  tracked ticker — the headline-only FinBERT path misattributes these) ·
  **· untracked** (about a company we don't follow) · **≡ digest**
  (a multi-company roundup like *Substantial Insider Sales: Morning Report* —
  the feed symbol is just one of dozens listed, so it's excluded from
  per-ticker sentiment and is **not** counted as a mismatch).
- **Bias** — bullish / bearish / neutral (the sign of the score).
- **Score** — composite sentiment, −1…+1 (formula below).
- **Mag** = **Magnitude** (1–5): how *material* the news is to the stock.
- **Nov** = **Novelty** (1–5): how *new / surprising* it is vs already-known.
- **Conf** = **Confidence** (1–5): the model's self-rated confidence it
  understood the article.
- **Arts** — how many articles were merged into this event (re-reports of one
  story collapse into a single event).

**How the score is computed** — deterministic: the LLM supplies the fields,
we do the arithmetic (so it's transparent and tunable):

```
sign      = +1 bullish / −1 bearish / 0 neutral
intensity = Magnitude / 5                     ← Mag drives the size
nov_mult  = 0.5 + 0.5 × (Novelty / 5)         ← already-known news discounted
score     = sign × intensity × nov_mult       (clamped to −1…+1)
```

- **Mag and Nov are inputs to the score.** e.g. bearish, Mag 4, Nov 3 →
  −1 × 0.80 × 0.80 = **−0.64**.
- **Conf is *not* in the score** — it's used only to pick which article's
  headline/summary to *display* as an event's representative.
- An **event's** score is the **mean** of all its articles' composite scores.
- The novelty floor (0.5) is `config.llm.novelty_discount_floor`.
"""
    )


def _sign_label(score) -> str:
    if pd.isna(score):
        return "—"
    return "bull" if score > 0.05 else ("bear" if score < -0.05 else "neut")


# Compact labels for the read-time attribution status (see models/llm_analyst.py).
_STATUS_LABEL = {
    "matched":      "✓ match",
    "reattributed": "↪ re-attr",
    "untracked":    "· untracked",
    "digest":       "≡ digest",
}


# ── Sidebar ───────────────────────────────────────────────────────────────────
opts = query_llm_analysis_options(days=90)
with st.sidebar:
    st.header("Filters")
    days = st.slider("Lookback (days)", 1, 90, 14)
    model_choices = ["(all)"] + opts["models"]
    model_sel = st.selectbox("Model", model_choices, index=0)
    model = None if model_sel == "(all)" else model_sel

df = query_llm_news_analysis(symbols=None, days=days, model=model)

if df.empty:
    st.info(
        "No LLM analysis yet for this window. Run the producers:\n\n"
        "1. `python scripts/ingest_news_bodies.py`  (needs IB Gateway)\n"
        "2. `python scripts/score_news_llm.py`  (needs Ollama running)\n\n"
        f"Configured model: **{config.llm.model}** · enabled: **{config.llm.enabled}**"
    )
    st.stop()

# ── Build the event-level frame (one row per event) ───────────────────────────
events_all = df[df["is_representative"]].copy()
# Digests cover dozens of companies — don't pin them to the feed symbol's
# sentiment (ticker stays NA so they drop out of the per-ticker views), but keep
# them in the table labelled as digests rather than as false mismatches.
_feed_or_attr = events_all["attributed_symbol"].fillna(events_all["symbol"])
events_all["ticker"] = _feed_or_attr.where(~events_all["is_digest"].astype(bool), other=pd.NA)
events_all["attrib"] = events_all["attribution_status"].map(_STATUS_LABEL).fillna("—")
events_all["score"] = events_all["event_score"]   # mean of all reads (score of record)
events_all["bias"] = events_all["score"].map(_sign_label)

# Events whose every read failed to parse have no score — exclude them from the
# table/aggregates (they'd render as nan) but keep the count visible.
n_articles = len(df)
n_unscored = int(events_all["score"].isna().sum())
events = events_all[events_all["score"].notna()].copy()
n_events = len(events)

# Remaining sidebar filters (built from what's present)
with st.sidebar:
    tickers_present = sorted(events["ticker"].dropna().unique().tolist())
    tick_sel = st.multiselect("Ticker (attributed)", tickers_present, default=[])
    min_abs = st.slider("Min |score|", 0.0, 1.0, 0.0, 0.05)
    min_mag = st.slider("Min magnitude", 1, 5, 1)
    _status_order = ["matched", "reattributed", "untracked", "digest"]
    _status_present = [s for s in _status_order
                       if s in set(events["attribution_status"].dropna())]
    status_sel = st.multiselect(
        "Attribution status", _status_present, default=[],
        format_func=lambda s: _STATUS_LABEL.get(s, s),
        help="matched = about the feed symbol · re-attr = about a different "
             "tracked ticker · untracked = a company we don't follow · "
             "digest = multi-company roundup (not a mismatch).")
    if st.button("↻ Refresh"):
        query_llm_news_analysis.clear()
        query_llm_analysis_options.clear()
        st.rerun()

# ── Summary cards ─────────────────────────────────────────────────────────────
mean_score = events["score"].mean()
n_bull = int((events["score"] > 0.05).sum())
n_bear = int((events["score"] < -0.05).sum())
n_neut = n_events - n_bull - n_bear
_status_counts = events["attribution_status"].value_counts()
n_reattr = int(_status_counts.get("reattributed", 0))
n_untracked = int(_status_counts.get("untracked", 0))
n_digest = int(_status_counts.get("digest", 0))

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Events", n_events, help=f"from {n_articles} articles")
c2.metric("Mean event score", f"{mean_score:+.2f}" if pd.notna(mean_score) else "—")
c3.metric("Bull / Bear / Neut", f"{n_bull} / {n_bear} / {n_neut}")
c4.metric("Re-attr / Untrk / Digest", f"{n_reattr} / {n_untracked} / {n_digest}",
          help="Re-attr = about a different tracked ticker (the value-add). "
               "Untrk = about a company we don't follow. Digest = multi-company "
               "roundup (excluded from per-ticker sentiment — NOT a mismatch).")
c5.metric("Dedup", f"{n_articles}→{n_events}",
          help="Articles collapsed into events (re-reports merged).")
st.divider()

# ── Section 1: Events table ───────────────────────────────────────────────────
st.subheader("Events")

filt = events.copy()
if tick_sel:
    filt = filt[filt["ticker"].isin(tick_sel)]
if status_sel:
    filt = filt[filt["attribution_status"].isin(status_sel)]
filt = filt[filt["score"].abs() >= min_abs]
filt = filt[filt["magnitude"].fillna(0) >= min_mag]

_unscored_note = (
    f" · {n_unscored} event(s) excluded — the model's output didn't parse "
    "(usually a truncated entities list on a multi-company digest; see Research → "
    "Parse failures)." if n_unscored else ""
)
st.caption(
    f"{len(filt)} of {n_events} events after filters. Sort by clicking a column "
    "header. **Ticker** = the company the article is *about* (resolved; blank for "
    "digests); **Feed** = the IBKR tag it arrived under; **Attrib** = ↪ re-attr "
    "(about a different tracked ticker), · untracked, or ≡ digest (multi-company "
    f"roundup — not a mismatch).{_unscored_note}"
)

table = filt[[
    "published_at", "ticker", "symbol", "attrib", "bias",
    "score", "magnitude", "novelty", "event_size", "event_type", "headline",
]].rename(columns={
    "published_at": "Published", "ticker": "Ticker", "symbol": "Feed",
    "attrib": "Attrib", "bias": "Bias", "score": "Score",
    "magnitude": "Mag", "novelty": "Nov", "event_size": "Arts",
    "event_type": "Type", "headline": "Headline",
})
st.dataframe(
    table, use_container_width=True, hide_index=True, height=380,
    column_config={
        "Published": st.column_config.DatetimeColumn(format="MM-DD HH:mm", width="small"),
        "Attrib": st.column_config.TextColumn(width="small",
            help="match / re-attr (other tracked ticker) / untracked / digest"),
        "Score": st.column_config.NumberColumn(format="%+.2f", width="small"),
        "Mag": st.column_config.NumberColumn(width="small"),
        "Nov": st.column_config.NumberColumn(width="small"),
        "Arts": st.column_config.NumberColumn(help="articles merged into this event", width="small"),
        "Headline": st.column_config.TextColumn(width="large"),
    },
)
st.divider()

# ── Section 2: Symbol drill-down ──────────────────────────────────────────────
st.subheader("Symbol drill-down")
sel = st.selectbox("Ticker", tickers_present,
                   index=0 if tickers_present else None)
if sel:
    sub = events[events["ticker"] == sel].sort_values("published_at")

    # sentiment time series (daily mean event score)
    ts = (sub.assign(day=sub["published_at"].dt.date)
          .groupby("day")["score"].mean().reset_index())
    fig = go.Figure(go.Scatter(
        x=ts["day"], y=ts["score"], mode="lines+markers",
        line=dict(color=TEAL), marker=dict(size=8)))
    fig.add_hline(y=0, line=dict(color=GREY, dash="dash"))
    fig.update_layout(
        template="plotly_dark", height=260,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="mean event score", yaxis_range=[-1, 1])
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Daily mean LLM sentiment for **{sel}** over the window. "
               "This is the view that turns months of rows into a trend.")

    # per-event detail
    st.markdown(f"**{len(sub)} event(s) for {sel}:**")
    for _, ev in sub.sort_values("published_at", ascending=False).iterrows():
        sc = ev["score"]
        badge = "🟢" if sc > 0.05 else ("🔴" if sc < -0.05 else "⚪")
        mism = " · ↪️ actually about " + str(ev["primary_entity"]) if ev["attribution_mismatch"] else ""
        n = int(ev["event_size"])
        title = f"{badge} {sc:+.2f}{mism} · {ev['published_at']:%m-%d} · {ev['headline'][:80]}"
        with st.expander(title):
            members = df[df["event_id"] == ev["event_id"]]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Event score (mean)", f"{sc:+.2f}")
            m2.metric("Articles", n)
            m3.metric("Spread", f"{ev['event_score_min']:+.2f}…{ev['event_score_max']:+.2f}"
                      if n > 1 else "—")
            m4.metric("Feed tag", ev["symbol"])
            st.markdown(f"**Summary:** {ev['summary'] or '—'}")
            st.markdown(f"**Rationale:** {ev['rationale'] or '—'}")
            try:
                ents = json.loads(ev["entities"]) if ev["entities"] else []
            except (ValueError, TypeError):
                ents = []
            st.markdown(f"**Entities:** {', '.join(ents) if ents else '—'}")
            if n > 1:
                st.caption(f"Event score is the mean of all {n} reads below "
                           "(the representative headline is shown above):")
            reads = members[["published_at", "composite_score", "direction",
                             "magnitude", "novelty", "confidence", "headline"]].rename(
                columns={"published_at": "Published", "composite_score": "Score",
                         "direction": "Dir", "magnitude": "Mag", "novelty": "Nov",
                         "confidence": "Conf", "headline": "Headline"})
            st.dataframe(
                reads, use_container_width=True, hide_index=True,
                column_config={
                    "Published": st.column_config.DatetimeColumn(format="MM-DD HH:mm", width="small"),
                    "Score": st.column_config.NumberColumn(format="%+.2f", width="small"),
                })

            # Full article text (lazy — fetched on demand, bodies are large)
            st.markdown("**Full article text** — the source the model scored:")
            opt_map = {
                f"{pd.to_datetime(r['published_at']):%m-%d %H:%M} · {(r['headline'] or '')[:75]}":
                    (r["symbol"], r["article_id"])
                for _, r in members.iterrows()
            }
            pick = st.selectbox("Article", list(opt_map.keys()),
                                key=f"bodysel_{ev['event_id']}",
                                label_visibility="collapsed")
            bsym, baid = opt_map[pick]
            body = query_news_body(bsym, baid)
            st.text_area(
                "body", body or "(full body not stored for this article — "
                "only IBKR-sourced articles with ≥800 chars are ingested)",
                height=300, key=f"bodytxt_{ev['event_id']}",
                label_visibility="collapsed")

st.divider()

# ── Section 3: Research (collapsed) ───────────────────────────────────────────
with st.expander("Research — distributions, cross-check & telemetry"):
    rc1, rc2 = st.columns(2)
    with rc1:
        st.markdown("**Event-score distribution**")
        fig = go.Figure(go.Histogram(x=events["score"], nbinsx=21,
                                     marker_color=TEAL, opacity=0.85))
        fig.update_layout(template="plotly_dark", height=280,
                          margin=dict(l=0, r=0, t=10, b=0),
                          xaxis_title="event score", yaxis_title="events")
        st.plotly_chart(fig, use_container_width=True)
    with rc2:
        st.markdown("**Composite vs LLM-direct (article level)**")
        cross = df.dropna(subset=["composite_score", "llm_direct_score"])
        if cross.empty:
            st.info("No rows with both scores.")
        else:
            fig = go.Figure(go.Scatter(
                x=cross["llm_direct_score"], y=cross["composite_score"],
                mode="markers", marker=dict(color=TEAL, size=7, opacity=0.6),
                text=cross["symbol"]))
            fig.add_shape(type="line", x0=-1, y0=-1, x1=1, y1=1,
                          line=dict(color=GREY, dash="dash"))
            fig.update_layout(template="plotly_dark", height=280,
                              margin=dict(l=0, r=0, t=10, b=0),
                              xaxis_title="LLM direct", yaxis_title="composite")
            st.plotly_chart(fig, use_container_width=True)

    t1, t2, t3, t4 = st.columns(4)
    t1.metric("Avg time / article", f"{df['duration_ms'].mean()/1000:.0f}s")
    t2.metric("Avg input tokens", f"{df['prompt_tokens'].mean():.0f}")
    t3.metric("Avg output tokens", f"{df['output_tokens'].mean():.0f}")
    t4.metric("Parse failures", int((~df["parse_ok"]).sum()))
