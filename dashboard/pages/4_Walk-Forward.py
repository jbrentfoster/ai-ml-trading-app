"""
Walk-Forward Validation — Page 4
==================================
Summary statistics and per-fold performance charts sourced from the
walk_forward_results and ensemble_weight_history SQLite tables.

Includes a "Run Walk-Forward Training" button that executes the full
ML pipeline directly from the UI (no terminal required).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config.settings import config
from data.ui_queries import (
    query_company_name,
    query_ensemble_weight_history,
    query_walk_forward_results,
    symbol_picker,
)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Walk-Forward",
    page_icon="📉",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Training helper ───────────────────────────────────────────────────────────

def _run_walk_forward(symbol: str, interval: str, quick_mode: bool) -> tuple[int, str]:
    """
    Run MLWalkForwardOrchestrator for `symbol`.

    In quick mode:
      - LSTM: 5 epochs instead of configured value
      - XGBoost: 50 estimators instead of configured value
      - Walk-forward: 2 folds instead of configured value

    Returns (n_folds, cache_dir_path).
    Raises on any error so the caller can show st.error().
    """
    from data.indicators import IndicatorEngine
    from models.walk_forward import MLWalkForwardOrchestrator

    # Snapshot and optionally override hyperparams
    saved = {}
    if quick_mode:
        saved = {
            "lstm_epochs":      config.ml.lstm_epochs,
            "xgb_n_estimators": config.ml.xgb_n_estimators,
            "wf_n_splits":      config.ml.wf_n_splits,
            "wf_train_bars":    config.ml.wf_train_bars,
            "wf_test_bars":     config.ml.wf_test_bars,
        }
        config.ml.lstm_epochs      = 5
        config.ml.xgb_n_estimators = 50
        config.ml.wf_n_splits      = 2
        config.ml.wf_train_bars    = 60   # 3 months; min = 60+1+2*10 = 81 bars
        config.ml.wf_test_bars     = 10

    try:
        engine = IndicatorEngine()
        df = engine.run(symbol, interval=interval)

        if df is None or df.empty:
            raise ValueError(
                f"No data for {symbol} ({interval}).  "
                "Run the data pipeline first (Market Data page → Refresh)."
            )

        orch    = MLWalkForwardOrchestrator(symbol)
        results = orch.run(df)

        cache_dir = Path(f"models/cache/{symbol}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        orch.save_models(cache_dir)

        return len(results), str(cache_dir)

    finally:
        # Always restore original config
        for k, v in saved.items():
            setattr(config.ml, k, v)


# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("📉 Walk-Forward Validation")
st.sidebar.markdown("---")

_default_sym = config.data.watchlist[0] if config.data.watchlist else "AAPL"
symbol = symbol_picker(
    "Symbol (or 'All')",
    default=_default_sym,
    key="wf_symbol",
    help="Pick from the current Stage 3 universe, or type any symbol — type 'All' to view every symbol.",
)

st.sidebar.markdown("**Run Training**")
train_sym = symbol_picker("Train symbol", default="AAPL", key="wf_train_sym")
train_iv   = st.sidebar.selectbox("Interval", ["1d", "1h"], key="wf_train_iv")
quick_mode = st.sidebar.checkbox(
    "Quick mode",
    value=True,
    key="wf_quick",
    help="5 LSTM epochs / 50 XGB trees / 2 folds — much faster, less accurate",
)

run_btn = st.sidebar.button("Run Walk-Forward Training", type="primary", key="wf_run")

st.sidebar.markdown("---")
if st.sidebar.button("Refresh cache", key="wf_refresh"):
    query_walk_forward_results.clear()
    query_ensemble_weight_history.clear()
    st.rerun()

# ── Execute training (runs before any display so results are visible immediately) ─

if run_btn:
    mode_label = "quick" if quick_mode else "full"
    with st.spinner(
        f"Running {mode_label} walk-forward training for **{train_sym}** "
        f"({train_iv}) … LSTM may take a couple of minutes."
    ):
        try:
            n_folds, cache_dir = _run_walk_forward(train_sym, train_iv, quick_mode)
            query_walk_forward_results.clear()
            query_ensemble_weight_history.clear()
            st.success(
                f"Training complete — {n_folds} fold(s) finished.  "
                f"Models saved to `{cache_dir}`."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Training failed: {exc}")

# ── Load results ──────────────────────────────────────────────────────────────

sym_filter = "" if symbol == "All" else symbol
wf_df      = query_walk_forward_results(sym_filter)
wt_df      = query_ensemble_weight_history()

_company = query_company_name(symbol) if symbol and symbol.upper() != "ALL" else ""
st.title(f"Walk-Forward Validation — {symbol}" + (f" ({_company})" if _company else ""))

# ── Empty state ───────────────────────────────────────────────────────────────

if wf_df.empty:
    st.info(
        "No walk-forward results yet.  "
        "Use **Run Walk-Forward Training** in the sidebar to train models "
        "and populate this page.  \n\n"
        "Quick mode completes in 1–3 minutes; full mode may take 10–20 minutes "
        "depending on dataset size."
    )
    st.stop()

# ── Survivorship-bias banner ──────────────────────────────────────────────────
# Rows tagged `universe_policy='dynamic'` came from a UniverseSelector-driven
# run — the candidate set was decided using today's data, so historical folds
# may include symbols that only became candidates in hindsight.  Warn loudly
# when any visible row is dynamic so the user discounts the metrics
# accordingly.  NULL ("unknown") rows predate the 2026-05-12 schema migration
# and don't trigger the banner.

if "Universe Policy" in wf_df.columns:
    policy_counts = wf_df["Universe Policy"].fillna("unknown").value_counts().to_dict()
    n_dynamic = int(policy_counts.get("dynamic", 0))
    n_static  = int(policy_counts.get("static", 0))
    n_unknown = int(policy_counts.get("unknown", 0))
    if n_dynamic > 0:
        msg = (
            f"⚠️ **Survivorship-bias warning** — {n_dynamic} of {len(wf_df)} "
            f"folds shown ran under a *dynamic* universe (UniverseSelector). "
            "The candidate pool was determined using **today's** data, not the "
            "data available at the start of each fold, so historical folds may "
            "include symbols that only became candidates in hindsight. "
            "Discount Sharpe / win-rate accordingly.  "
            "For unbiased backtests, set `universe.enabled = False` (Settings → Universe) "
            "or run training with `--use-watchlist`."
        )
        if n_static > 0 or n_unknown > 0:
            msg += (
                f"\n\nBreakdown: {n_dynamic} dynamic · {n_static} static · "
                f"{n_unknown} unknown (pre-2026-05-12)."
            )
        st.warning(msg)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SUMMARY CARDS
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Summary")

n_windows  = len(wf_df)
pos_sharpe = (wf_df["Sharpe Ratio"].dropna() > 0).sum()
pct_pos    = pos_sharpe / n_windows * 100 if n_windows else 0.0
avg_sharpe = wf_df["Sharpe Ratio"].mean()
avg_dd     = wf_df["Max Drawdown"].mean()
avg_wr     = wf_df["Win Rate"].mean()

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Windows Completed",  n_windows)
c2.metric("% Positive Sharpe",  f"{pct_pos:.0f}%")
c3.metric("Avg Sharpe Ratio",   f"{avg_sharpe:.3f}" if pd.notna(avg_sharpe) else "—")
c4.metric("Avg Max Drawdown",   f"{avg_dd:.1%}"     if pd.notna(avg_dd)     else "—")
c5.metric("Avg Win Rate",       f"{avg_wr:.1%}"     if pd.notna(avg_wr)     else "—")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SHARPE BAR CHART
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Sharpe Ratio per Window")
st.caption(
    "The **Sharpe ratio** measures risk-adjusted return: annualized strategy return divided by annualized volatility "
    "of the daily bar-by-bar returns within each test window.  "
    "A Sharpe > 1.0 is generally considered good; > 2.0 is excellent; < 0 means the strategy lost money "
    "on a risk-adjusted basis in that fold.  "
    "Walk-forward testing applies the strategy strictly out-of-sample — the model never sees the test window "
    "during training — so positive Sharpe here is meaningful, not curve-fitted.  "
    "The dashed yellow line at 1.0 is the design target."
)

def _fold_label(row) -> str:
    ts = row.get("Test Start")
    te = row.get("Test End")
    if pd.notna(ts) and pd.notna(te):
        return (f"Fold {int(row['Fold'])}  "
                f"({pd.Timestamp(ts).strftime('%b %y')}–{pd.Timestamp(te).strftime('%b %y')})")
    return f"Fold {int(row['Fold'])}"

wf_df["Window Label"] = wf_df.apply(_fold_label, axis=1)

sharpe_colors = [
    "#26a69a" if (pd.notna(v) and v >= 0) else "#ef5350"
    for v in wf_df["Sharpe Ratio"]
]

sharpe_fig = go.Figure(go.Bar(
    x=wf_df["Window Label"],
    y=wf_df["Sharpe Ratio"],
    marker_color=sharpe_colors,
    text=[f"{v:.2f}" if pd.notna(v) else "—" for v in wf_df["Sharpe Ratio"]],
    textposition="outside",
))
sharpe_fig.add_hline(y=1.0, line_dash="dash",
                     line_color="rgba(255,255,0,0.5)",
                     annotation_text="Target: Sharpe 1.0",
                     annotation_position="top right")
sharpe_fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)")
sharpe_fig.update_layout(
    height=360, template="plotly_dark",
    yaxis=dict(title="Sharpe Ratio"),
    margin=dict(l=0, r=0, t=20, b=0),
)
st.plotly_chart(sharpe_fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — MAX DRAWDOWN LINE CHART
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Max Drawdown per Window")
st.caption(
    "**Maximum drawdown** is the largest peak-to-trough decline in cumulative returns within each test window — "
    "the worst-case loss an investor holding the strategy would have experienced.  "
    "The values are negative (a 10% drawdown is shown as -10%).  Smaller magnitude is better.  "
    "A drawdown beyond -20% in a fold suggests the position sizing or stop-loss settings may need tightening.  "
    "Drawdown complements the Sharpe ratio: a strategy can have a good Sharpe but still suffer sharp short-term losses."
)

dd_fig = go.Figure(go.Scatter(
    x=wf_df["Window Label"],
    y=wf_df["Max Drawdown"].fillna(0) * 100,
    mode="lines+markers",
    line=dict(color="#ef5350", width=2),
    marker=dict(size=8, color="#ef5350"),
    fill="tozeroy",
    fillcolor="rgba(239,83,80,0.1)",
))
dd_fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)")
dd_fig.update_layout(
    height=280, template="plotly_dark",
    yaxis=dict(title="Max Drawdown (%)"),
    margin=dict(l=0, r=0, t=20, b=0),
)
st.plotly_chart(dd_fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — ENSEMBLE WEIGHT EVOLUTION
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Ensemble Weight Evolution")
st.caption(
    "After each fold the ensemble **rebalances** based on out-of-sample performance.  "
    "Whichever of LSTM and XGBoost had the higher Sharpe ratio in the test window gains up to 10% weight "
    "(the 'nudge'), while the other loses the same amount — both are floored at 10% so no model is fully discarded.  "
    "FinBERT is excluded from this competition (its 'evaluate' is news-coverage-based, not Sharpe-based); "
    "instead, its weight scales with the fraction of test-window bars that had a non-zero sentiment score — "
    "folds with sparse news coverage automatically reduce FinBERT's influence.  "
    "A growing slice indicates that model has been consistently outperforming in recent folds."
)

if wt_df.empty:
    st.info("No ensemble weight history yet — weights are recorded after each fold rebalance.")
else:
    # Use a sequential rebalance index as x-axis.  All rebalances within a single
    # training run happen within seconds of each other, so using recorded_at causes
    # Plotly to zoom into a one-second window and appear empty.
    wt_df = wt_df.reset_index(drop=True)
    wt_df["Rebalance #"] = wt_df.index + 1
    hover_ts = wt_df["recorded_at"].dt.strftime("%Y-%m-%d %H:%M").tolist()

    wt_fig = go.Figure()
    model_colors = {"LSTM": "#2196f3", "XGBoost": "#26a69a", "FinBERT": "#ff9800"}
    for model, color in model_colors.items():
        if model in wt_df.columns:
            wt_fig.add_trace(go.Scatter(
                x=wt_df["Rebalance #"],
                y=wt_df[model] * 100,
                name=model,
                mode="lines+markers",
                line=dict(color=color, width=2),
                marker=dict(size=6),
                stackgroup="one",
                customdata=hover_ts,
                hovertemplate="%{y:.1f}%  —  %{customdata}<extra>%{fullData.name}</extra>",
            ))
    wt_fig.update_layout(
        height=300, template="plotly_dark",
        yaxis=dict(title="Weight (%)", range=[0, 100]),
        xaxis=dict(title="Rebalance #", dtick=1, tick0=1),
        legend=dict(orientation="h", y=1.05, x=0),
        margin=dict(l=0, r=0, t=20, b=0),
    )
    st.plotly_chart(wt_fig, use_container_width=True)
    st.caption("Stacked area — hover over any point for the exact rebalance timestamp.")

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — DETAILED RESULTS TABLE
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Detailed Results")

table_cols = [
    "Symbol", "Fold", "Window Label",
    "Train Start", "Train End", "Test Start", "Test End",
    "Total Return", "Ann. Return", "Sharpe Ratio",
    "Max Drawdown", "Win Rate", "# Signals",
    "Universe Policy", "Sentiment Note",
]
table_cols = [c for c in table_cols if c in wf_df.columns]

def _sharpe_color(val) -> str:
    if pd.isna(val):
        return ""
    return "color: #26a69a" if val >= 0 else "color: #ef5350"

def _policy_color(val) -> str:
    if pd.isna(val) or val is None:
        return "color: rgba(255,255,255,0.4)"   # unknown — grey
    if str(val) == "dynamic":
        return "color: #ffb74d"                  # amber — biased
    return "color: #26a69a"                      # static — clean

display_df = wf_df[table_cols].copy()
if "Sentiment Note" in display_df.columns:
    display_df["Sentiment Note"] = display_df["Sentiment Note"].fillna("—")
if "Universe Policy" in display_df.columns:
    display_df["Universe Policy"] = display_df["Universe Policy"].fillna("unknown")

styled = display_df.style.map(_sharpe_color, subset=["Sharpe Ratio"])
if "Universe Policy" in display_df.columns:
    styled = styled.map(_policy_color, subset=["Universe Policy"])
styled = styled.format({
    "Total Return": "{:.2%}",
    "Ann. Return":  "{:.2%}",
    "Sharpe Ratio": "{:.3f}",
    "Max Drawdown": "{:.2%}",
    "Win Rate":     "{:.1%}",
}, na_rep="—")

st.dataframe(
    styled,
    use_container_width=True,
    height=min(40 + len(wf_df) * 38, 500),
)

csv_bytes = wf_df.to_csv(index=False).encode("utf-8")
st.download_button("Download results CSV", csv_bytes,
                   file_name="walk_forward_results.csv", mime="text/csv")

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    f"Full-mode config: {config.ml.wf_train_bars} train bars · "
    f"{config.ml.wf_test_bars} test bars · "
    f"{config.ml.wf_gap_bars} gap bar(s) · "
    f"{config.ml.wf_n_splits} splits "
    f"(min {config.ml.wf_train_bars + config.ml.wf_gap_bars + config.ml.wf_test_bars} bars required)  |  "
    f"Quick mode: 60 train / 10 test / 2 folds / 5 epochs / 50 trees (min 81 bars)"
)
