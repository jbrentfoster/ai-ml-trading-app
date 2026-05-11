"""
Page 7 — Universe Selection

Shows the automated stock universe funnel: how thousands of listed equities
are filtered down to a short, high-quality candidate list through three
progressively tighter stages.
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

from config.settings import config
from data.ui_queries import query_universe_assets, query_universe_run_log

st.set_page_config(page_title="Universe", page_icon="🌐", layout="wide")
st.title("Universe Selection")

st.markdown(
    "The **automated universe** replaces the static watchlist with a "
    "dynamic, regularly refreshed candidate list. A three-stage funnel "
    "narrows ~5,000 listed equities down to a focused set of liquid, "
    "fundamental-quality names — plus permanent fixtures like index ETFs "
    "and sector funds that are always included."
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Universe Settings")
    universe_enabled = config.universe.enabled
    st.metric("Universe mode", "Enabled" if universe_enabled else "Disabled")
    if not universe_enabled:
        st.info(
            "Universe selection is currently **disabled**. "
            "The pipeline uses the static watchlist from Settings. "
            "To enable, set `universe.enabled = true` in Settings > ML Models "
            "or run `python universe_scheduler.py --run-now`."
        )
    st.caption(
        f"Stage 1 max: {config.universe.stage1_max:,} | "
        f"Stage 2 max: {config.universe.stage2_max:,} | "
        f"Stage 3 max: {config.universe.stage3_max:,}"
    )
    st.caption(
        f"Min market cap: ${config.universe.min_market_cap/1e9:.1f}B | "
        f"Min avg $ volume: ${config.universe.min_avg_dollar_volume/1e6:.0f}M/day"
    )
    if st.button("Refresh cache"):
        query_universe_assets.clear()
        query_universe_run_log.clear()
        st.rerun()

    st.divider()
    st.subheader("Manual Controls")
    st.caption(
        "These buttons run the universe selector directly from the dashboard. "
        "For scheduled automation, use `universe_scheduler.py`."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        run_full_btn = st.button("Full Refresh", use_container_width=True)
    with col_b:
        rescore_btn = st.button("Re-score Stage 3", use_container_width=True)

    if run_full_btn:
        with st.spinner("Running full universe refresh (Stage 1–3)..."):
            try:
                from data.universe import UniverseSelector
                result = UniverseSelector().run_full()
                query_universe_assets.clear()
                query_universe_run_log.clear()
                st.success(
                    f"Done: {result.stage1_count} → {result.stage2_count} "
                    f"→ {result.stage3_count} candidates  "
                    f"({result.duration_seconds:.1f}s)"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Full refresh failed: {exc}")

    if rescore_btn:
        with st.spinner("Re-scoring Stage 3..."):
            try:
                from data.universe import UniverseSelector
                result = UniverseSelector().run_rescore()
                query_universe_assets.clear()
                query_universe_run_log.clear()
                st.success(
                    f"Done: {result.stage3_count} candidates  "
                    f"({result.duration_seconds:.1f}s)"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Re-score failed: {exc}")


# ── Load data ─────────────────────────────────────────────────────────────────

df_active   = query_universe_assets(active_only=True)
df_all      = query_universe_assets(active_only=False)
df_run_log  = query_universe_run_log(limit=200)

# ── Section 1: Funnel overview ────────────────────────────────────────────────

st.header("Funnel Overview")
st.markdown(
    "Each stage narrows the candidate pool. "
    "Numbers show the most recent run."
)

# Derive stage counts from run log.
# Rescores only log Stage 3 — use the most recent *full* run for Stage 1/2,
# and the most recent run of any type for Stage 3.
if not df_run_log.empty and "Stage" in df_run_log.columns:
    def _latest_count(stage, run_type_filter=None):
        subset = df_run_log
        if run_type_filter:
            subset = subset[subset["Type"] == run_type_filter]
        subset = subset[subset["Stage"] == stage]
        return int(subset["Count"].iloc[0]) if not subset.empty else 0

    s1 = _latest_count(1, run_type_filter="full")
    s2 = _latest_count(2, run_type_filter="full")
    s3 = _latest_count(3)   # most recent Stage 3 (may be a rescore)
else:
    s1 = s2 = s3 = 0

col1, col2, col3 = st.columns(3)
col1.metric("Stage 1 — Listed Universe",    f"{s1:,}" if s1 else "—",
            help="Active, tradable US equities from Alpaca")
col2.metric("Stage 2 — Liquidity Filter",   f"{s2:,}" if s2 else "—",
            help=f"Market cap ≥ ${config.universe.min_market_cap/1e9:.1f}B AND "
                 f"avg daily $ vol ≥ ${config.universe.min_avg_dollar_volume/1e6:.0f}M")
col3.metric("Stage 3 — Final Candidates",   f"{s3:,}" if s3 else "—",
            help="Top symbols by 20-day return + avg dollar volume (rank-percentile blend)")

if s1 > 0:
    fig_funnel = go.Figure(go.Funnel(
        y=["Stage 1\nListed Universe", "Stage 2\nLiquidity Filter", "Stage 3\nFinal Candidates"],
        x=[s1, s2, s3],
        textinfo="value+percent initial",
        marker_color=["#455a64", "#26a69a", "#00897b"],
    ))
    fig_funnel.update_layout(
        template="plotly_dark",
        margin=dict(l=0, r=0, t=40, b=0),
        height=320,
        title="Selection Funnel — Most Recent Run",
    )
    st.plotly_chart(fig_funnel, use_container_width=True)
    st.caption(
        "**How to read this chart:** each bar shows how many symbols survived "
        "that stage. Stage 1 starts with all active, tradable US equities on "
        "Alpaca. Stage 2 drops anything below the market-cap and dollar-volume "
        "thresholds. Stage 3 ranks the survivors on a 50/50 blend of 20-day "
        "return percentile and average-dollar-volume percentile, then keeps "
        "the top `stage3_max`. Permanent fixtures — index and sector ETFs — "
        "are always included regardless of score."
    )
else:
    st.info(
        "No universe run data found. Click **Full Refresh** in the sidebar "
        "or run `python universe_scheduler.py --run-now` to populate."
    )

# ── Section 2: Active candidates ──────────────────────────────────────────────

st.header("Active Candidates")

if df_active.empty:
    st.info(
        "No active universe candidates in the database yet. "
        "Run a full refresh to populate the list."
    )
else:
    n_fixtures = int(df_active["Fixture"].sum()) if "Fixture" in df_active.columns else 0
    st.caption(
        f"{len(df_active)} active symbols  |  "
        f"{n_fixtures} permanent fixtures  |  "
        f"{len(df_active) - n_fixtures} funnel candidates"
    )

    # Format for display
    display_cols = [c for c in
                    ["Symbol", "Name", "Class", "Fixture", "Stage",
                     "Market Cap", "Avg $ Volume", "Stage 3 Score", "Added", "Last Scored"]
                    if c in df_active.columns]
    df_show = df_active[display_cols].copy()

    if "Market Cap" in df_show.columns:
        df_show["Market Cap"] = df_show["Market Cap"].apply(
            lambda v: f"${v/1e9:.1f}B" if pd.notna(v) and v else "—"
        )
    if "Avg $ Volume" in df_show.columns:
        df_show["Avg $ Volume"] = df_show["Avg $ Volume"].apply(
            lambda v: f"${v/1e6:.1f}M" if pd.notna(v) and v else "—"
        )
    if "Stage 3 Score" in df_show.columns:
        df_show["Stage 3 Score"] = df_show["Stage 3 Score"].apply(
            lambda v: f"{v:.3f}" if pd.notna(v) and v is not None else "—"
        )

    st.dataframe(df_show, use_container_width=True, hide_index=True)
    st.caption(
        "**Symbol** — ticker. "
        "**Fixture** — always included (index ETFs, sector funds, etc.). "
        "**Stage** — last funnel stage reached. "
        "**Market Cap** — from yfinance fundamentals (24h cache). "
        "**Avg $ Volume** — average daily dollar volume = "
        "(close price × volume).mean() over most-recent 20 trading days. "
        "**Stage 3 Score** — `0.5 × pct_rank(20d_return) + 0.5 × pct_rank(ADV)` "
        "in [0, 1]; higher = stronger combination of recent momentum and liquidity."
    )


# ── Section 3: Universe size history ──────────────────────────────────────────

st.header("Universe Size History")

if df_run_log.empty:
    st.info("No run history yet. Run a full refresh to start tracking over time.")
else:
    # One data point per run × stage
    df_hist = df_run_log.copy()
    if "Stage" in df_hist.columns and "Recorded At" in df_hist.columns:
        df_hist["Recorded At"] = pd.to_datetime(df_hist["Recorded At"])

        fig_hist = go.Figure()
        colors   = {1: "#455a64", 2: "#26a69a", 3: "#00897b"}
        labels   = {1: "Stage 1", 2: "Stage 2", 3: "Stage 3 (Final)"}
        for stage in [1, 2, 3]:
            sub = df_hist[df_hist["Stage"] == stage].sort_values("Recorded At")
            if sub.empty:
                continue
            fig_hist.add_trace(go.Scatter(
                x=sub["Recorded At"],
                y=sub["Count"],
                mode="lines+markers",
                name=labels[stage],
                line_color=colors[stage],
            ))
        fig_hist.update_layout(
            template="plotly_dark",
            margin=dict(l=0, r=0, t=40, b=0),
            height=320,
            title="Candidate Count per Stage Over Time",
            xaxis_title="Run Date",
            yaxis_title="Symbol Count",
        )
        st.plotly_chart(fig_hist, use_container_width=True)
        st.caption(
            "**How to read this chart:** each point is one universe run. "
            "The three lines track how many symbols passed each stage. "
            "A shrinking Stage-3 line may indicate tightening fundamentals "
            "or fewer liquid names meeting the filter thresholds. "
            "Permanent fixtures are always counted in Stage 3."
        )


# ── Section 4: Recently removed ───────────────────────────────────────────────

st.header("Recently Removed")

if not df_all.empty and "Removed" in df_all.columns:
    df_removed = df_all[df_all["Removed"].notna()].copy()
    if not df_removed.empty:
        df_removed["Removed"] = pd.to_datetime(df_removed["Removed"])
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=30)
        df_recent = df_removed[df_removed["Removed"] >= cutoff].sort_values(
            "Removed", ascending=False
        )
        if df_recent.empty:
            st.info("No symbols removed in the last 30 days.")
        else:
            show_cols = [c for c in ["Symbol", "Name", "Stage", "Market Cap", "Removed"]
                         if c in df_recent.columns]
            st.dataframe(df_recent[show_cols], use_container_width=True, hide_index=True)
            st.caption(
                "Symbols that were active in a previous run but did not make "
                "it through the funnel in the most recent run. They may have "
                "dropped below the liquidity threshold or been out-ranked in "
                "Stage 3."
            )
    else:
        st.info("No removed symbols on record.")
else:
    st.info("No removal history available yet.")


# ── Section 5: Run log ────────────────────────────────────────────────────────

with st.expander("Run Log", expanded=False):
    if df_run_log.empty:
        st.info("No run log entries yet.")
    else:
        st.dataframe(df_run_log, use_container_width=True, hide_index=True)
        st.caption(
            "Each row represents one stage of one universe run. "
            "**Type** is 'full' (all three stages) or 'rescore' (Stage 3 only). "
            "**Duration** is wall-clock time for that stage in seconds."
        )
