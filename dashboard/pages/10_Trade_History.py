"""
Page 10 — Trade History

Closed-trade outcomes from the `trade_log` table.  Phase 4.5 Phase A populates
rows with `source='walk_forward'` from the bracket simulator; Phase B will add
`source='live'` rows from IBKR fill subscriptions.

Sections:
  1. Summary cards (n_trades, gross/net P&L, costs, win rate)
  2. Tax-impact view (short-term vs long-term, indicative-only)
  3. Trades table (color-coded net P&L)
  4. Charts (cumulative net P&L over time, exit-reason mix)
  5. Per-symbol breakdown
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from data.database import get_trade_log
from data.ui_queries import (
    query_bars,
    query_benchmark_returns,
    query_capital_weighted_roi,
    query_distinct_trade_log_run_ids,
    query_indicator_history,
    query_tax_breakdown,
    query_trade_forensics,
    query_trade_log,
    query_trade_log_filter_options,
    query_trade_summary,
)
from models.trade_patterns import (
    EntryContext,
    SEVERITY_WARNING,
    SEVERITY_CAUTION,
    evaluate as evaluate_patterns,
    group_by_bucket,
)
from config.settings import config as _app_config

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Trade History",
    page_icon="📒",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Trade History")
st.markdown(
    "Closed trades from the bracket simulator (walk-forward) and live fills.  "
    "Net P&L = gross P&L minus commissions and slippage.  Holding-period "
    "classification (short-term ≤ 365 days vs long-term > 365 days) feeds the "
    "indicative tax view below."
)

# ── Filter options (driven by what's actually in trade_log) ───────────────────
# The dedup + active-universe checkboxes below share session_state keys with
# this query so the dropdown lists only symbols/reasons present in the
# *current* view.  Streamlit reruns the script top-to-bottom on every
# interaction, so reading session_state here picks up the previous run's
# checkbox values (defaults True on first load).

dedup_default            = st.session_state.get("trade_history_dedup", True)
active_universe_default  = st.session_state.get("trade_history_active_universe", True)
opts = query_trade_log_filter_options(
    dedup_to_latest_run=dedup_default,
    active_universe_only=active_universe_default,
)
all_symbols      = opts["symbols"]
all_exit_reasons = opts["exit_reasons"]
all_sources      = opts["sources"]

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Filters")

    # Source — default to 'live' if any live rows exist, else 'walk_forward'.
    if "live" in all_sources:
        source_default = "live"
    elif "walk_forward" in all_sources:
        source_default = "walk_forward"
    else:
        source_default = "Both"
    source_choice = st.radio(
        "Source",
        options=["walk_forward", "live", "Both"],
        index=["walk_forward", "live", "Both"].index(source_default)
              if source_default in ["walk_forward", "live", "Both"] else 0,
        help=(
            "**walk_forward** = simulated trades from the WF bracket simulator.  "
            "**live** = IBKR paper/live fills (lands once Phase B ships).  "
            "**Both** = combined view."
        ),
    )
    source_filter = None if source_choice == "Both" else source_choice

    selected_symbols = st.multiselect(
        "Symbols",
        options=all_symbols,
        default=[],
        help="Empty = all symbols.",
    )

    today = date.today()
    default_start = today - timedelta(days=365)
    date_range = st.date_input(
        "Exit-date range",
        value=(default_start, today),
        help="Filters by exit_ts (the trade's realisation date).",
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = default_start, today

    selected_reasons = st.multiselect(
        "Exit reasons",
        options=all_exit_reasons,
        default=[],
        help="Empty = all exit reasons.  stop / tp / trailing / signal_flip / fold_end / manual_close.",
    )

    # Active-universe toggle — defaults ON because walk_forward_results
    # never deletes rows when a symbol leaves the universe, so the deduped
    # view keeps surfacing trades from symbols the system no longer tracks.
    # OFF = full historical record across all symbols ever trained (useful
    # for auditing universe-rotation effects).  Live rows always pass
    # through regardless — actual broker fills must remain in the historical
    # record for tax purposes even when a symbol rotates out.
    active_universe_only = st.checkbox(
        "Active universe only",
        value=True,
        key="trade_history_active_universe",
        help=(
            "ON (default): drop walk_forward rows for symbols not in the "
            "current active universe.  OFF: include every symbol that has "
            "ever been trained.  Live rows always pass through (broker fills "
            "stay in the history regardless).  Also filters the dropdowns "
            "above to match."
        ),
    )

    # Dedup toggle — defaults ON because every weekly --force retrain inserts
    # a fresh batch of WF trades with new run_ids; without dedup the page
    # stacks every historical run on top of itself and inflates summary cards.
    # Off = full multi-run history (useful for auditing model drift over time).
    dedup_to_latest = st.checkbox(
        "Dedupe to latest run per symbol",
        value=True,
        key="trade_history_dedup",
        help=(
            "ON (default): for walk_forward rows, keep only the latest training "
            "run per symbol — current model only.  OFF: show every weekly "
            "retrain stacked together (Nx duplicates after N runs).  Has no "
            "effect on live rows or when a specific Run ID is selected below.  "
            "Also filters the Symbols / Exit reasons dropdowns above to match."
        ),
    )

    # Run-ID dropdown — populated from trade_log directly so we only show
    # run_ids that actually exist.  Different scope from Page 8's run_ids
    # (those come from order_decisions / signal_runner runs).
    run_id_options = query_distinct_trade_log_run_ids(limit=30)
    if run_id_options:
        ALL_RUNS = "(all runs)"
        run_id_choices = [ALL_RUNS] + [r["run_id"] for r in run_id_options]
        run_id_meta = {r["run_id"]: r for r in run_id_options}

        def _format_run(rid: str) -> str:
            if rid == ALL_RUNS:
                return ALL_RUNS
            meta = run_id_meta[rid]
            ts   = meta["latest"].strftime("%Y-%m-%d %H:%M") if meta["latest"] else "—"
            return f"{rid[:8]}…  {ts}  ({meta['source']}, {meta['n_trades']} trades)"

        run_id_choice = st.selectbox(
            "Run ID",
            options=run_id_choices,
            index=0,
            format_func=_format_run,
            help=(
                "Filter to a single training/signal run.  These are walk-forward "
                "training run_ids today; once Phase B lands they include live "
                "signal_runner run_ids too.  Different scope from Page 8's run_ids "
                "(those come from order_decisions)."
            ),
        )
        run_id_filter = "" if run_id_choice == ALL_RUNS else run_id_choice
    else:
        run_id_filter = ""
        st.caption("Run ID filter populates after the first walk-forward training run.")

    st.divider()
    st.subheader("Tax rates (indicative)")
    st.caption("Personal — not persisted to YAML.")

    if "trade_history_st_rate" not in st.session_state:
        st.session_state["trade_history_st_rate"] = 24.0
    if "trade_history_lt_rate" not in st.session_state:
        st.session_state["trade_history_lt_rate"] = 15.0
    if "trade_history_state_rate" not in st.session_state:
        st.session_state["trade_history_state_rate"] = 0.0

    st_rate_pct = st.number_input(
        "Federal short-term %",
        min_value=0.0, max_value=60.0,
        step=0.5,
        key="trade_history_st_rate",
        help="Default 24% — single-filer middle bracket.  Short-term gains taxed as ordinary income.",
    )
    lt_rate_pct = st.number_input(
        "Federal long-term %",
        min_value=0.0, max_value=40.0,
        step=0.5,
        key="trade_history_lt_rate",
        help="Default 15% — middle long-term capital-gains bracket.",
    )
    state_rate_pct = st.number_input(
        "State %",
        min_value=0.0, max_value=15.0,
        step=0.5,
        key="trade_history_state_rate",
        help="Flat state rate applied to both ST and LT gains.  0 if your state has no income tax.",
    )

    st.divider()
    if st.button("Refresh cache", use_container_width=True):
        query_trade_log.clear()
        query_trade_summary.clear()
        query_tax_breakdown.clear()
        query_trade_log_filter_options.clear()
        query_distinct_trade_log_run_ids.clear()
        query_benchmark_returns.clear()
        query_capital_weighted_roi.clear()
        st.rerun()


# ── Build query kwargs ────────────────────────────────────────────────────────

filter_kwargs = dict(
    source=source_filter,
    symbols=tuple(selected_symbols) if selected_symbols else None,
    start_date=start_date,
    end_date=end_date,
    exit_reasons=tuple(selected_reasons) if selected_reasons else None,
    run_id=run_id_filter,
    dedup_to_latest_run=dedup_to_latest,
    active_universe_only=active_universe_only,
)

trades_df = query_trade_log(**filter_kwargs)

# ── Empty state ───────────────────────────────────────────────────────────────

if not all_symbols:
    st.info(
        "No trades logged yet.  The `trade_log` table is populated by:\n\n"
        "  - **Walk-forward training** (simulated): "
        "`python scripts/train_models.py` or the **Run Walk-Forward Training** button on Page 4.\n"
        "  - **Live fills** (Phase B — pending): IBKR `execDetails` subscription "
        "writing rows once `signal_runner.py --no-dry-run` produces real fills."
    )
    st.stop()

if trades_df.empty:
    st.warning(
        "No trades match the current filters.  Widen the date range or clear "
        "the symbol / exit-reason filters in the sidebar."
    )
    # Diagnostic: echo the active filters + the unfiltered row count so an
    # empty result is debuggable.  The raw (dedup-off) set is always a
    # superset of the deduped set, so toggling dedup OFF can never empty a
    # non-empty deduped view on its own — if this fires after unchecking
    # dedup, look at Source / Run ID / date range below, not the dedup box.
    _unfiltered = query_trade_log(
        source=source_filter, dedup_to_latest_run=False, active_universe_only=False,
    )
    st.caption(
        f"Active filters → source=`{source_filter or 'Both'}`, "
        f"symbols=`{list(selected_symbols) or 'all'}`, "
        f"exit_reasons=`{list(selected_reasons) or 'all'}`, "
        f"dates=`{start_date} … {end_date}`, "
        f"run_id=`{run_id_filter or 'all'}`, "
        f"dedup=`{dedup_to_latest}`, active_universe=`{active_universe_only}`.  "
        f"With **all filters cleared except Source**, `trade_log` holds "
        f"**{len(_unfiltered):,}** `{source_filter or 'any-source'}` row(s).  "
        "If that count is 0, the **Source** radio is the cause "
        "(`live` has no rows until Phase B writes a paired round trip)."
    )
    st.stop()

# ── 1. Summary cards ──────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("Summary")

summary = query_trade_summary(**filter_kwargs)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Closed Trades",   f"{summary['n_trades']:,}")
c2.metric("Gross P&L",       f"${summary['gross_pnl']:,.2f}")
c3.metric("Total Fees",      f"${summary['total_costs']:,.2f}")
c4.metric(
    "Net P&L",
    f"${summary['net_pnl']:,.2f}",
    delta=f"{summary['net_pnl'] - summary['gross_pnl']:,.2f} fees impact",
    delta_color="inverse",
)
c5.metric("Win Rate (net)",  f"{summary['win_rate']:.1%}")

st.caption(
    "**Win rate** counts a trade as a win only if `net_pnl > 0` — fees count "
    "against you.  A trade that nets to exactly zero after fees is treated as a loss."
)

# ── 1a. Capital-Weighted ROI vs benchmark (live fills only) ───────────────────
#
# Answers "did the money I actually deployed beat just holding the benchmark?"
# Forced to source='live' regardless of the sidebar Source radio — walk_forward
# rows are Kelly-sized synthetic trades against an assumed equity base, so their
# notional is not real capital and summing it produces a meaningless ROI base.
#
# Capital-weighted (Σ pnl / Σ shares×entry_px) deliberately differs from the
# unweighted per-trade percentage view in section 1b below: a $50k position and
# a $500 position count by their dollars here, equally there.  Strategy side is
# NET of fees; benchmark side is RAW — the retail-alpha frame (a buy-and-hold
# benchmark pays no commissions).  See ui_queries.query_capital_weighted_roi.

_bench_sym = _app_config.data.benchmark_symbol

st.markdown("---")
st.subheader(f"Capital-Weighted ROI vs {_bench_sym} (live fills)")

roi = query_capital_weighted_roi(
    symbols=tuple(selected_symbols) if selected_symbols else None,
    start_date=start_date,
    end_date=end_date,
    exit_reasons=tuple(selected_reasons) if selected_reasons else None,
    run_id=run_id_filter,
    dedup_to_latest_run=dedup_to_latest,
    active_universe_only=active_universe_only,
)

if roi["n_trades"] == 0:
    st.info(
        f"No live trades with {_bench_sym} benchmark data in the current filter.  "
        "This section is scoped to `source='live'` rows only (real broker fills) "
        "regardless of the **Source** radio in the sidebar — walk-forward "
        "simulated trades are excluded because their notional isn't real "
        "capital.  Populated by Phase B fill reconciliation + the Flex backfill; "
        "rows missing `benchmark_return_pct` are excluded (run "
        "`python scripts/backfill_benchmark_returns.py` to fill them in)."
    )
elif roi["capital_deployed"] <= 0:
    st.warning(
        f"{roi['n_trades']:,} live trade(s) found, but total deployed capital is "
        f"${roi['capital_deployed']:,.2f} — can't form an ROI denominator.  "
        f"Dollar edge vs {_bench_sym}: ${roi['dollar_diff']:+,.2f}."
    )
else:
    rc1, rc2, rc3 = st.columns(3)
    rc1.metric(
        "Strategy ROI (net)",
        f"{roi['strategy_roi']:+.2%}",
        delta=(
            f"${roi['strategy_pnl']:+,.0f} on ${roi['capital_deployed']:,.0f} "
            f"deployed · {roi['n_trades']:,} trades"
        ),
        delta_color="off",
    )
    rc2.metric(
        f"{_bench_sym} ROI (same capital)",
        f"{roi['benchmark_roi']:+.2%}",
        delta=(
            f"${roi['benchmark_pnl']:+,.0f} hypothetical buy & hold "
            f"on the same dollars"
        ),
        delta_color="off",
    )
    rc3.metric(
        f"Edge vs {_bench_sym}",
        f"${roi['dollar_diff']:+,.2f}",
        delta=f"{roi['roi_diff_pct']:+.2%} ROI",
        delta_color="normal",  # green when strategy beat the benchmark, red when it lagged
    )

    lead = "beat" if roi["dollar_diff"] >= 0 else "lagged"
    st.caption(
        f"**Did my money beat {_bench_sym}?**  For every dollar actually "
        f"deployed across {roi['n_trades']:,} live fill(s), the strategy "
        f"returned **{roi['strategy_roi']:+.2%} net of fees**; the same capital "
        f"held in {_bench_sym} over each trade's holding period would have "
        f"returned **{roi['benchmark_roi']:+.2%}** (raw, no fees).  The strategy "
        f"**{lead}** the benchmark by **${roi['dollar_diff']:+,.2f}** "
        f"({roi['roi_diff_pct']:+.2%}).  Each trade's benchmark counterfactual "
        f"uses *its own* deployed capital (`shares × entry_px`), so big "
        f"positions move this number more than small ones — unlike the unweighted "
        f"per-trade percentage view in the section below.  Strategy P&L is net of "
        f"commissions; the {_bench_sym} side is raw because a buy-and-hold "
        f"position pays no trading fees (the honest retail-alpha frame)."
    )

# ── 1b. Benchmark-Relative Performance ────────────────────────────────────────
#
# Headline metrics + chart EXCLUDE ``exit_reason='fold_end'`` — those rows are
# backtest artifacts (the WF test window's last bar forced the close), not
# strategy decisions the live system would ever make.  Including them masks
# the alpha picture because the fold_end subset is left-truncated toward
# winners-still-running (positions whose brackets had not fired by test_end).
# The per-trade audit expander offers a toggle to include them so the user
# can verify the filter is honest.  See CLAUDE.md "Fold-end closures are
# backtest artifacts, not strategy decisions" for the diagnosis.

st.markdown("---")
st.subheader(f"Benchmark-Relative Performance (vs {_app_config.data.benchmark_symbol})")

bench_df = query_benchmark_returns(**filter_kwargs)

if bench_df.empty:
    st.info(
        "Not enough data yet to compute meaningful benchmark-relative metrics.  "
        "Need at least 5 trades with benchmark data.  Run "
        "`python scripts/backfill_benchmark_returns.py` if existing trades are "
        "missing benchmark returns."
    )
else:
    strategy_df = bench_df[bench_df["exit_reason"] != "fold_end"].copy()
    fold_end_df = bench_df[bench_df["exit_reason"] == "fold_end"].copy()

    if len(strategy_df) < 5:
        st.info(
            f"Only {len(strategy_df)} strategy-decided trade(s) with benchmark "
            "data after excluding fold-end rows — need at least 5 for meaningful "
            "metrics.  Widen the date range or clear other sidebar filters."
        )
    else:
        n_strategy = len(strategy_df)
        n_fold_end = len(fold_end_df)

        # Per-trade distribution stats (all in % units, not fractions)
        excess_series_pct = strategy_df["excess_pct"] * 100.0
        cum_excess_pct = float(excess_series_pct.sum())
        avg_excess_pct = float(excess_series_pct.mean())
        med_excess_pct = float(excess_series_pct.median())
        std_excess_pct = float(excess_series_pct.std())
        p25_excess_pct = float(excess_series_pct.quantile(0.25))
        p75_excess_pct = float(excess_series_pct.quantile(0.75))
        n_wins_vs_b   = int((strategy_df["excess_pct"] > 0).sum())
        win_vs_b_pct  = 100.0 * n_wins_vs_b / n_strategy
        mean_median_gap = avg_excess_pct - med_excess_pct

        bc1, bc2, bc3 = st.columns(3)
        bc1.metric(
            "Avg Excess per Trade",
            f"{avg_excess_pct:+.3f}%",
            delta=f"± {std_excess_pct:.2f}% std across {n_strategy:,} trades",
            delta_color="off",
        )
        bc2.metric(
            "Median Excess per Trade",
            f"{med_excess_pct:+.3f}%",
            delta=("half above / half below"
                   if abs(med_excess_pct) < 0.05
                   else f"typical trade {'beats' if med_excess_pct > 0 else 'lags'} benchmark"),
            delta_color="off",
        )
        bc3.metric(
            "Win Rate vs Benchmark",
            f"{win_vs_b_pct:.1f}%",
            delta=f"{n_wins_vs_b:,} / {n_strategy:,} trades beat benchmark",
            delta_color="off",
        )

        # Diagnostic flag: mean-vs-median spread reveals whether alpha is
        # broad-based or driven by a few outlier wins.  2pp threshold picked
        # to match the std-dispersion floor below which symmetric noise is
        # expected; above it, the distribution is meaningfully right-skewed.
        if abs(mean_median_gap) >= 2.0:
            direction = "right" if mean_median_gap > 0 else "left"
            polarity = "winners" if mean_median_gap > 0 else "losers"
            st.warning(
                f"**Outlier-driven (mean − median = {mean_median_gap:+.2f}pp)** — "
                f"distribution is {direction}-skewed; alpha is concentrated in "
                f"a small number of large {polarity}.  "
                f"Top 25% of trades beat benchmark by ≥{p75_excess_pct:+.2f}%; "
                f"bottom 25% lag by ≤{p25_excess_pct:+.2f}%.  "
                f"If the big {polarity} stop coming, the headline avg will "
                f"collapse toward the median ({med_excess_pct:+.2f}%)."
            )
        else:
            st.success(
                f"**Distribution looks symmetric (mean − median = {mean_median_gap:+.2f}pp)** — "
                f"mean and median agree, so the per-trade alpha estimate is broad-based "
                f"rather than driven by a few outlier trades."
            )

        st.caption(
            "**Headline metrics computed over strategy-decided exits only** "
            f"(stop / tp / signal_flip / trailing — {n_strategy:,} trades).  "
            f"Excludes {n_fold_end:,} fold-end forced closures — those are backtest "
            "artifacts of WF test-window boundaries, not real exit decisions the live "
            "system would ever make.  Toggle the expander below to audit the filter.  "
            f"**Sum of per-trade excess** (NOT a portfolio compound return): "
            f"{cum_excess_pct:+.2f}% — informative as a trajectory in the cumulative "
            "chart below, but the per-trade stats above are the honest alpha estimate."
        )

        # ── Per-trade excess distribution histogram ───────────────────────
        # Visualises the same data the cards summarise — bulk vs tails, skew,
        # mean-median gap.  Skipped below n=20 since histograms are unstable
        # at low n and the cards already cover the small-sample case.
        import math
        if n_strategy >= 20:
            # Bin edges aligned to multiples of 2 so the 0% benchmark line
            # always falls on a bin boundary (no half-positive / half-negative
            # bars that would confuse the red/teal split).
            bin_size = 2.0
            x_min = math.floor(excess_series_pct.min() / bin_size) * bin_size
            x_max = math.ceil (excess_series_pct.max() / bin_size) * bin_size
            xbins_full = dict(start=x_min, end=x_max, size=bin_size)

            # Split at 0 for the red/teal colour convention (matches rest of page).
            neg_excess = excess_series_pct[excess_series_pct <  0]
            pos_excess = excess_series_pct[excess_series_pct >= 0]

            hist_fig = go.Figure()

            # IQR (p25-p75) shaded band — sits behind the bars.
            hist_fig.add_vrect(
                x0=p25_excess_pct, x1=p75_excess_pct,
                fillcolor="rgba(255,255,255,0.08)",
                line_width=0,
                layer="below",
                annotation_text=f"IQR (middle 50%): {p25_excess_pct:+.1f}% to {p75_excess_pct:+.1f}%",
                annotation_position="top left",
                annotation_font_size=11,
                annotation_font_color="rgba(255,255,255,0.55)",
            )

            if not neg_excess.empty:
                hist_fig.add_trace(go.Histogram(
                    x=neg_excess, xbins=xbins_full,
                    marker_color="#ef5350",
                    marker_line=dict(width=0.5, color="rgba(0,0,0,0.4)"),
                    name=f"Lagged benchmark ({len(neg_excess):,})",
                    hovertemplate="excess %{x}%<br>trades: %{y}<extra></extra>",
                ))
            if not pos_excess.empty:
                hist_fig.add_trace(go.Histogram(
                    x=pos_excess, xbins=xbins_full,
                    marker_color="#26a69a",
                    marker_line=dict(width=0.5, color="rgba(0,0,0,0.4)"),
                    name=f"Beat benchmark ({len(pos_excess):,})",
                    hovertemplate="excess %{x}%<br>trades: %{y}<extra></extra>",
                ))

            # Annotation lines: benchmark (0), median, mean.
            # Stagger label positions so they don't overlap when mean ≈ median.
            hist_fig.add_vline(
                x=0,
                line_color="rgba(255,255,255,0.55)",
                line_width=1.5,
                line_dash="dash",
                annotation_text="benchmark (0%)",
                annotation_position="top",
                annotation_font_color="rgba(255,255,255,0.75)",
            )
            hist_fig.add_vline(
                x=med_excess_pct,
                line_color="#ffb74d",
                line_width=2,
                annotation_text=f"median {med_excess_pct:+.2f}%",
                annotation_position="top left" if med_excess_pct >= avg_excess_pct else "bottom left",
                annotation_font_color="#ffb74d",
            )
            hist_fig.add_vline(
                x=avg_excess_pct,
                line_color="#ffd54f",
                line_width=2,
                annotation_text=f"mean {avg_excess_pct:+.2f}%",
                annotation_position="top right" if avg_excess_pct >= med_excess_pct else "bottom right",
                annotation_font_color="#ffd54f",
            )

            hist_fig.update_layout(
                height=340,
                template="plotly_dark",
                xaxis=dict(title=f"Excess return per trade vs {_app_config.data.benchmark_symbol} (%)"),
                yaxis=dict(title="Number of trades"),
                margin=dict(l=0, r=0, t=40, b=0),
                barmode="overlay",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                bargap=0.05,
            )
            st.plotly_chart(hist_fig, use_container_width=True)
            st.caption(
                f"Distribution of per-trade excess returns vs {_app_config.data.benchmark_symbol} "
                f"across {n_strategy:,} strategy-decided trades, in 2% bins.  "
                "**Read the shape**: bars to the left of the dashed white line lagged the "
                "benchmark; bars to the right beat it.  The shaded band marks the middle "
                "50% of trades (IQR).  A **healthy alpha distribution** is roughly symmetric "
                "around 0% — mean and median sit close together.  When the **mean line sits "
                "well to the right of the median**, the strategy depends on a small number "
                "of large winners (right-skewed); the typical trade is closer to the median "
                "than to the headline mean."
            )
        else:
            st.info(
                f"Distribution histogram skipped — only {n_strategy:,} trades available "
                "(need at least 20 for the bin counts to be meaningful).  Widen the date "
                "range or clear sidebar filters to see more trades."
            )

        # ── Cumulative strategy vs benchmark chart ────────────────────────
        # Plot the two component lines (strategy / benchmark) whose vertical
        # gap IS the cumulative excess.  Same units for all three series —
        # cumulative SUM of per-trade % — so excess == strategy − benchmark
        # holds pointwise and the shaded gap reads as the alpha band.
        _benchmark_sym = _app_config.data.benchmark_symbol
        cum_excess_df = strategy_df.sort_values("exit_ts")[
            ["exit_ts", "pnl_pct", "benchmark_return_pct", "excess_pct"]
        ].copy()
        cum_excess_df["cum_strategy_pct"] = cum_excess_df["pnl_pct"].cumsum() * 100.0
        cum_excess_df["cum_benchmark_pct"] = (
            cum_excess_df["benchmark_return_pct"].cumsum() * 100.0
        )
        cum_excess_df["cum_excess_pct"] = cum_excess_df["excess_pct"].cumsum() * 100.0

        final_cum = float(cum_excess_df["cum_excess_pct"].iloc[-1])
        line_color = "#26a69a" if final_cum >= 0 else "#ef5350"
        fill_color = (
            "rgba(38,166,154,0.15)" if final_cum >= 0 else "rgba(239,83,80,0.15)"
        )

        excess_fig = go.Figure()
        # Benchmark first (no fill) so the strategy trace can fill 'tonexty'
        # down/up to it — the shaded band between the two lines is the excess.
        excess_fig.add_trace(go.Scatter(
            x=cum_excess_df["exit_ts"],
            y=cum_excess_df["cum_benchmark_pct"],
            mode="lines",
            name=f"{_benchmark_sym} (benchmark)",
            line=dict(color="#90a4ae", width=2, dash="dot"),
        ))
        excess_fig.add_trace(go.Scatter(
            x=cum_excess_df["exit_ts"],
            y=cum_excess_df["cum_strategy_pct"],
            mode="lines",
            name="Strategy",
            line=dict(color=line_color, width=2),
            fill="tonexty",
            fillcolor=fill_color,
        ))
        excess_fig.add_hline(y=0, line_color="rgba(255,255,255,0.4)", line_width=1)
        excess_fig.update_layout(
            height=340, template="plotly_dark",
            yaxis=dict(title="Cumulative return (%)"),
            xaxis=dict(title="Exit date"),
            margin=dict(l=0, r=0, t=20, b=0),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="right", x=1),
        )
        st.plotly_chart(excess_fig, use_container_width=True)
        st.caption(
            "**Trajectory only — not a portfolio compound return.**  "
            f"Two lines: cumulative *sum* of per-trade strategy returns vs the "
            f"cumulative sum of {_benchmark_sym} returns over the same holding "
            "periods, for **strategy-decided exits** (stop, tp, signal_flip, "
            "trailing).  **The shaded gap between them is the cumulative excess** "
            f"({final_cum:+.1f}%) — green when the strategy leads {_benchmark_sym}, "
            "red when it trails.  Magnitude scales linearly with trade count, so "
            "read the *gap's shape* (widening / flat / narrowing), not the endpoint "
            "value — the per-trade stats in the cards above are the honest alpha "
            "estimate.  Fold-end forced closures excluded — backtest artifacts, not "
            "real exit decisions.  A persistently widening gap in the strategy's "
            f"favour is evidence of alpha; lines that track together mean no edge "
            f"over simply holding {_benchmark_sym}."
        )

        # ── Per-exit-reason excess breakdown ──────────────────────────────
        st.markdown("**Excess return by exit reason**")
        per_reason = (
            bench_df.groupby("exit_reason", dropna=False)
                    .agg(n=("excess_pct", "size"),
                         avg_excess=("excess_pct", "mean"))
                    .reset_index()
        )
        per_reason["avg_excess_pct"] = per_reason["avg_excess"] * 100.0
        # Sort by count desc; fold_end always at the bottom for visual separation.
        per_reason["_sort_key"] = per_reason["exit_reason"].apply(
            lambda r: (1 if r == "fold_end" else 0, -per_reason.loc[
                per_reason["exit_reason"] == r, "n"
            ].iloc[0])
        )
        per_reason = per_reason.sort_values("_sort_key").drop(
            columns=["_sort_key", "avg_excess"]
        ).reset_index(drop=True)
        per_reason["exit_reason_display"] = per_reason["exit_reason"].apply(
            lambda r: f"{r}  (excluded from metrics)" if r == "fold_end" else r
        )
        per_reason_display = per_reason[["exit_reason_display", "n", "avg_excess_pct"]].rename(
            columns={"exit_reason_display": "Exit Reason",
                     "n":              "Trades",
                     "avg_excess_pct": "Avg Excess vs Benchmark"}
        )

        def _excess_row_style(row: pd.Series) -> list[str]:
            if "fold_end" in str(row["Exit Reason"]):
                return ["color: rgba(255,255,255,0.45); font-style: italic"] * len(row)
            avg = row["Avg Excess vs Benchmark"]
            if avg > 0:
                return ["color: #26a69a"] * len(row)
            if avg < 0:
                return ["color: #ef5350"] * len(row)
            return [""] * len(row)

        styled_reason = (
            per_reason_display.style
            .apply(_excess_row_style, axis=1)
            .format({"Trades": "{:,}", "Avg Excess vs Benchmark": "{:+.3f}%"})
        )
        st.dataframe(styled_reason, use_container_width=True, hide_index=True)
        st.caption(
            "Rows are coloured by sign of avg excess.  Look for the bucket "
            "with the most trades AND the most negative excess — that's where "
            "alpha is bleeding."
        )

        # ── Per-trade audit expander ──────────────────────────────────────
        with st.expander("🔎  Per-trade excess return details"):
            include_fold_end = st.checkbox(
                "Include fold_end rows (backtest artifacts)",
                value=False,
                key="bench_include_fold_end",
                help=(
                    "Default OFF — fold_end exits are forced closures at WF "
                    "test-window boundaries, not strategy decisions the live "
                    "system would make.  Toggle ON to audit the filter: the "
                    "table will show all benchmark-eligible rows including the "
                    f"{n_fold_end:,} fold_end trades, so you can see exactly "
                    "what's excluded from the headline metrics above.  Headline "
                    "cards and chart are NOT affected by this toggle."
                ),
            )

            audit_df = bench_df if include_fold_end else strategy_df
            audit_cols = {
                "symbol":               "Symbol",
                "entry_ts":             "Entry Date",
                "exit_ts":              "Exit Date",
                "holding_days":         "Days",
                "pnl_pct":              "Trade Return %",
                "benchmark_return_pct": "Benchmark Return %",
                "excess_pct":           "Excess %",
                "exit_reason":          "Exit Reason",
            }
            audit_display = audit_df[list(audit_cols.keys())].rename(columns=audit_cols).copy()
            audit_display["Trade Return %"]     *= 100.0
            audit_display["Benchmark Return %"] *= 100.0
            audit_display["Excess %"]           *= 100.0

            def _excess_cell_style(val: float) -> str:
                if pd.isna(val):
                    return ""
                if val > 0:
                    return "background-color: #1b3a2a"
                if val < 0:
                    return "background-color: #3a1b1b"
                return ""

            audit_styled = (
                audit_display.style
                .apply(
                    lambda row: [
                        _excess_cell_style(row["Excess %"]) if c == "Excess %" else ""
                        for c in audit_display.columns
                    ],
                    axis=1,
                )
                .format({
                    "Days":               "{:,.0f}",
                    "Trade Return %":     "{:+.2f}%",
                    "Benchmark Return %": "{:+.2f}%",
                    "Excess %":           "{:+.2f}%",
                })
            )
            st.dataframe(audit_styled, use_container_width=True, hide_index=True, height=420)
            st.caption(
                f"Showing {len(audit_display):,} row(s) — "
                f"{'including' if include_fold_end else 'excluding'} fold_end."
            )

            audit_csv = audit_display.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download benchmark-relative trades as CSV",
                data=audit_csv,
                file_name=(
                    f"benchmark_relative_"
                    f"{'incl' if include_fold_end else 'excl'}_foldend_"
                    f"{start_date}_{end_date}.csv"
                ),
                mime="text/csv",
            )

# ── 2. Tax-impact view ────────────────────────────────────────────────────────

st.markdown("---")
with st.expander("📋  Tax Impact (indicative — not tax advice)", expanded=True):
    st.markdown(
        "> **Disclaimer.** This is an *indicative* view computed from `trade_log` "
        "rows. It is **not tax advice** and may differ materially from broker-reported "
        "figures. IBKR's 1099-B is the authoritative record. Wash-sale adjustments "
        "(IRC §1091), lot-level cost-basis methods, AMT, and the 3.8% Net Investment "
        "Income Tax are **not** modelled here. WF-simulated trades are not taxable "
        "events at all — switch to `live` source for the only meaningful number."
    )

    tax = query_tax_breakdown(**filter_kwargs)

    # Effective rate per class (federal + state)
    st_rate_eff = (st_rate_pct + state_rate_pct) / 100.0
    lt_rate_eff = (lt_rate_pct + state_rate_pct) / 100.0

    # Tax estimate uses *net* per class (intra-class offset).  Approximation:
    # the IRS lets cross-class offsets with specific ordering — we don't model
    # that here.  Only positive net positions generate tax; negative nets carry
    # forward as a separate line item.
    st_taxable = max(tax["st_net"], 0.0)
    lt_taxable = max(tax["lt_net"], 0.0)
    est_tax = st_taxable * st_rate_eff + lt_taxable * lt_rate_eff
    carryforward = -min(tax["total_net"], 0.0)

    tc1, tc2, tc3 = st.columns(3)
    tc1.metric(
        f"Short-term (≤365d) net   [{tax['n_st']:,} trades]",
        f"${tax['st_net']:,.2f}",
        delta=f"+${tax['st_gain']:,.0f} gains  /  −${tax['st_loss']:,.0f} losses",
        delta_color="off",
    )
    tc2.metric(
        f"Long-term (>365d) net   [{tax['n_lt']:,} trades]",
        f"${tax['lt_net']:,.2f}",
        delta=f"+${tax['lt_gain']:,.0f} gains  /  −${tax['lt_loss']:,.0f} losses",
        delta_color="off",
    )
    tc3.metric(
        "Estimated tax owed",
        f"${est_tax:,.2f}",
        delta=(
            f"ST {st_rate_eff:.1%} on ${st_taxable:,.0f}  +  "
            f"LT {lt_rate_eff:.1%} on ${lt_taxable:,.0f}"
        ),
        delta_color="off",
    )

    if carryforward > 0:
        st.warning(
            f"Net realised position is a **loss of ${carryforward:,.2f}** — no tax owed. "
            "Real-world treatment: up to $3,000/yr offsets ordinary income; the rest "
            "carries forward indefinitely.  Talk to a tax pro before relying on this."
        )

    st.caption(
        "Within-class offset only: ST losses reduce ST gains, LT losses reduce LT gains.  "
        "Cross-class offset (LT loss → ST gain, etc.) and the IRS netting order are "
        "not modelled — this number is an upper bound on tax owed for typical cases."
    )

# ── 3. Trades table ───────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("Closed Trades")

display_df = trades_df.rename(columns={
    "symbol":        "Symbol",
    "signal":        "Signal",
    "entry_ts":      "Entry Date",
    "exit_ts":       "Exit Date",
    "holding_days":  "Days",
    "shares":        "Shares",
    "entry_px":      "Entry $",
    "exit_px":       "Exit $",
    # ``pnl`` is already net of costs (see ui_queries.query_trade_log
    # docstring).  Display the back-derived ``gross_pnl`` under "Gross P&L"
    # and the stored ``pnl`` (== net_pnl) under "Net P&L".
    "gross_pnl":     "Gross P&L",
    "costs_charged": "Fees",
    "net_pnl":       "Net P&L",
    "pnl_pct":       "P&L %",
    "exit_reason":   "Exit Reason",
    "source":        "Source",
    "run_id":        "Run ID",
    "recorded_at":   "Recorded At",
})
display_df["ST/LT"]  = display_df["Days"].apply(lambda d: "LT" if d > 365 else "ST")
display_df["Run ID"] = display_df["Run ID"].fillna("").astype(str).str.slice(0, 8)
display_df["P&L %"]  = display_df["P&L %"] * 100  # store as percent for display

final_cols = [
    "Symbol", "Signal", "Entry Date", "Exit Date", "Days", "ST/LT",
    "Shares", "Entry $", "Exit $",
    "Gross P&L", "Fees", "Net P&L", "P&L %",
    "Exit Reason", "Source", "Run ID", "Recorded At",
]
final_cols = [c for c in final_cols if c in display_df.columns]


def _net_pnl_color(val: float) -> str:
    if pd.isna(val):
        return ""
    if val > 0:
        return "background-color: #1b3a2a"   # dark teal-green
    if val < 0:
        return "background-color: #3a1b1b"   # dark red
    return ""


styled = (
    display_df[final_cols]
    .style.apply(
        lambda row: [_net_pnl_color(row["Net P&L"]) if c == "Net P&L" else ""
                     for c in final_cols],
        axis=1,
    )
    .format({
        "Shares":    "{:,.0f}",
        "Entry $":   "${:,.2f}",
        "Exit $":    "${:,.2f}",
        "Gross P&L": "${:,.2f}",
        "Fees":      "${:,.2f}",
        "Net P&L":   "${:,.2f}",
        "P&L %":     "{:+.2f}%",
        "Days":      "{:,.0f}",
    }, na_rep="—")
)

st.dataframe(styled, use_container_width=True, hide_index=True, height=420)

# CSV export — use the numeric display_df, not the styled one.
csv_bytes = display_df[final_cols].to_csv(index=False).encode("utf-8")
st.download_button(
    "Download trades as CSV",
    data=csv_bytes,
    file_name=f"trade_history_{start_date}_{end_date}.csv",
    mime="text/csv",
)

# ── 4. Charts ─────────────────────────────────────────────────────────────────

st.markdown("---")
chart_l, chart_r = st.columns([3, 2])

with chart_l:
    st.subheader("Cumulative Net P&L")
    st.caption(
        "Running total of net P&L by exit date.  Sloping up = strategy is making money "
        "after fees in this window; flat or down = it isn't.  Compare this against the "
        "*gross* curve in your head — the gap is what fees cost you."
    )

    cum_df = trades_df.sort_values("exit_ts")[["exit_ts", "net_pnl", "gross_pnl"]].copy()
    cum_df["Net Cumulative"]   = cum_df["net_pnl"].cumsum()
    cum_df["Gross Cumulative"] = cum_df["gross_pnl"].cumsum()

    cum_fig = go.Figure()
    cum_fig.add_trace(go.Scatter(
        x=cum_df["exit_ts"], y=cum_df["Gross Cumulative"],
        mode="lines",
        name="Gross",
        line=dict(color="rgba(255,255,255,0.4)", width=1, dash="dot"),
    ))
    cum_fig.add_trace(go.Scatter(
        x=cum_df["exit_ts"], y=cum_df["Net Cumulative"],
        mode="lines",
        name="Net (after fees)",
        line=dict(color="#26a69a", width=2),
        fill="tozeroy",
        fillcolor="rgba(38,166,154,0.15)",
    ))
    cum_fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)")
    cum_fig.update_layout(
        height=360, template="plotly_dark",
        yaxis=dict(title="Cumulative P&L ($)"),
        xaxis=dict(title="Exit date"),
        margin=dict(l=0, r=0, t=20, b=0),
        legend=dict(orientation="h", y=1.05, x=0),
    )
    st.plotly_chart(cum_fig, use_container_width=True)

with chart_r:
    st.subheader("Exit-Reason Mix")
    st.caption(
        "How trades closed.  Heavy **stop** = stops are doing the work; heavy **fold_end** "
        "= bracket exits rarely fired (look at win rate before celebrating)."
    )
    reason_counts = (
        trades_df["exit_reason"].value_counts().rename_axis("Reason").reset_index(name="Count")
    )

    reason_color_map = {
        "tp":           "#26a69a",   # green
        "trailing":     "#2196f3",   # blue
        "signal_flip":  "#9575cd",   # purple
        "fold_end":     "#90a4ae",   # grey
        "stop":         "#ef5350",   # red
        "manual_close": "#ff9800",   # amber
    }
    reason_colors = [reason_color_map.get(r, "#cccccc") for r in reason_counts["Reason"]]

    donut_fig = go.Figure(go.Pie(
        labels=reason_counts["Reason"],
        values=reason_counts["Count"],
        hole=0.55,
        marker=dict(colors=reason_colors),
        textinfo="label+percent",
    ))
    donut_fig.update_layout(
        height=360, template="plotly_dark",
        margin=dict(l=0, r=0, t=20, b=0),
        showlegend=False,
    )
    st.plotly_chart(donut_fig, use_container_width=True)

    with st.expander("What do these exit reasons mean?"):
        st.markdown(
            "- **stop** — Fixed ATR stop hit (long: `entry − atr_stop_mult × ATR`).  "
            "Intra-bar fills get extra slippage (`stop_slippage_multiplier × slippage_pct`); "
            "gap-through fills at `Open` without the extra charge (the gap *is* the slippage).  "
            "If both stop and TP sit inside the bar's range on the same bar, the stop wins "
            "(worst-case rule).\n"
            "- **tp** — Fixed ATR take-profit limit hit at `entry + atr_tp_mult × ATR`.  "
            "Limit fills are exact (no slippage).  Removed once a trailing stop activates.\n"
            "- **trailing** — Trailing stop hit *after* activation.  Once price moves "
            "`activation_atr × ATR` favourably, brackets are replaced with `peak − trail_atr × ATR`; "
            "the peak ratchets with each bar's High and the new level only applies *next* bar "
            "(today's High doesn't tighten today's stop).\n"
            "- **signal_flip** — Gate emitted the opposite signal mid-trade.  Long held → SELL "
            "signal at bar close → position closed at that close.  A new opposite-direction trade "
            "is scheduled for the next bar's open, **but only if `allow_short_selling=True`**.  "
            "Under the live default (False), a SELL after a long is close-only — no short opens.\n"
            "- **fold_end** — *Walk-forward only.*  Each fold's test window (~21 bars) runs in "
            "isolation.  If a position is still open at the last bar — no stop, no TP, no opposite "
            "signal — it gets force-flattened at that bar's close.  Without this rule, positions "
            "would bleed across fold boundaries and contaminate the next fold's training data with "
            "implicit lookahead.  Will never appear in `source='live'` rows.  Heavy `fold_end` in "
            "the donut means brackets rarely fired — either ATR multipliers are too wide, the "
            "test window is too short relative to typical holding periods, or signals are sparse.  "
            "Cross-check win rate before drawing conclusions.\n"
            "- **manual_close** — *Live only* (reserved).  Position closed *outside* the "
            "strategy's normal lifecycle — operator manually flattened (e.g. "
            "`scripts/open_positions.py --close`), or some off-strategy event terminated the "
            "trade.  Not produced by the WF simulator; appears once Phase B (live fill "
            "subscription) lands."
        )

# ── 5. Per-symbol breakdown ───────────────────────────────────────────────────

st.markdown("---")
with st.expander("📊  Per-symbol breakdown"):
    sym_groups = []
    for sym, g in trades_df.groupby("symbol"):
        n           = len(g)
        wins        = int((g["net_pnl"] > 0).sum())
        win_rate    = wins / n if n else 0.0
        # ``gross_pnl`` is the back-derived column from query_trade_log;
        # ``pnl`` would mistakenly equal net here (see double-counting bug).
        gross_pnl   = float(g["gross_pnl"].sum())
        costs       = float(g["costs_charged"].fillna(0.0).sum())
        net_pnl     = float(g["net_pnl"].sum())
        avg_hold    = float(g["holding_days"].mean())
        st_net = float(
            g.loc[~g["is_long_term"], "net_pnl"].sum()
        )
        lt_net = float(
            g.loc[ g["is_long_term"], "net_pnl"].sum()
        )
        sym_groups.append({
            "Symbol":      sym,
            "Trades":      n,
            "Win Rate":    win_rate,
            "Avg Days":    avg_hold,
            "Gross P&L":   gross_pnl,
            "Fees":        costs,
            "Net P&L":     net_pnl,
            "ST Net":      st_net,
            "LT Net":      lt_net,
        })
    sym_df = pd.DataFrame(sym_groups).sort_values("Net P&L", ascending=False)
    if sym_df.empty:
        st.info("No symbols in current filter.")
    else:
        styled_sym = (
            sym_df.style.apply(
                lambda row: [
                    _net_pnl_color(row["Net P&L"]) if c == "Net P&L" else ""
                    for c in sym_df.columns
                ],
                axis=1,
            )
            .format({
                "Win Rate":  "{:.1%}",
                "Avg Days":  "{:.0f}",
                "Gross P&L": "${:,.2f}",
                "Fees":      "${:,.2f}",
                "Net P&L":   "${:,.2f}",
                "ST Net":    "${:,.2f}",
                "LT Net":    "${:,.2f}",
            })
        )
        st.dataframe(styled_sym, use_container_width=True, hide_index=True)


# ── 6. Trade Forensics (per-trade drill-down) ─────────────────────────────────
#
# Anchored on a single closed trade: reconstruct the model's decision context at
# entry, its score trajectory across the hold, the exit attribution, and the
# "left on the table" counterfactual.  Bridges the gap between Page 3 (symbol-
# and-time centric scores) and this page's outcome view.

st.markdown("---")
st.subheader("🔍  Trade Forensics")
st.caption(
    "Pick a symbol, then a closed trade, to see *why* the model entered and how "
    "its conviction evolved until the exit.  Entry-side context is reconstructed "
    "from the `signal_log` row that drove the entry; bracket-fired exits "
    "(stop/tp/trailing) have no model decision at the off-cycle fill instant — "
    "so for those the question is 'what was the model saying while we held?'"
)

_forensic_symbols = sorted(trades_df["symbol"].dropna().unique().tolist())
if not _forensic_symbols:
    st.info("No trades in the current filter to analyse.")
    st.stop()

fcol1, fcol2 = st.columns([1, 3])
with fcol1:
    f_symbol = st.selectbox("Symbol", options=_forensic_symbols, key="forensic_symbol")

sym_trades = trades_df[trades_df["symbol"] == f_symbol].copy()
# Newest first by exit date.
sym_trades = sym_trades.sort_values("exit_ts", ascending=False).reset_index(drop=True)


def _trade_label(r: pd.Series) -> str:
    ed = pd.Timestamp(r["entry_ts"]).date()
    xd = pd.Timestamp(r["exit_ts"]).date()
    pct = (r["pnl_pct"] or 0.0) * 100.0
    bench = r.get("benchmark_return_pct")
    excess = ""
    if pd.notna(bench):
        excess = f" · {pct - bench * 100.0:+.1f}% vs {_app_config.data.benchmark_symbol}"
    return (f"{r['signal']} {ed} → {xd} · {r['exit_reason']} · "
            f"{pct:+.1f}%{excess} · [{r['source']}]")


with fcol2:
    pos = st.selectbox(
        "Trade",
        options=list(range(len(sym_trades))),
        format_func=lambda i: _trade_label(sym_trades.iloc[i]),
        key="forensic_trade",
    )

sel = sym_trades.iloc[pos]

# ── Window / data controls (must run BEFORE the forensics assembler) ──────────
# A symbol that rotated out of the universe stops getting bars fetched, so the
# chart can end well before today.  These two controls let you (a) widen the
# chart window to today and (b) backfill the missing bars from yfinance.  The
# fetch is an explicit user-triggered network action — the same escape-hatch
# pattern as Page 2's "Fetch & Score News" and Page 6's IBKR refresh.
wc1, wc2 = st.columns([1, 1])
with wc1:
    through_today = st.checkbox(
        "Extend chart through today",
        value=False,
        key="forensic_through_today",
        help="Widen the chart's right edge to today instead of stopping a few "
             "bars after the exit.  Only shows bars already in the DB — if the "
             "symbol rotated out of the universe you'll see a gap; use the "
             "fetch button to fill it.",
    )
with wc2:
    if st.button("🔄 Fetch latest bars from yfinance",
                 key="forensic_fetch_bars",
                 help=f"One-off yfinance pull to backfill {f_symbol}'s daily bars "
                      "up to today (rotated-out symbols aren't tracked by the "
                      "pipeline).  Writes to the DB; safe to re-run."):
        from data.fetcher import DataFetcher
        _gap_days = (pd.Timestamp.now().normalize()
                     - pd.Timestamp(sel["exit_ts"]).normalize()).days + 10
        with st.spinner(f"Fetching {f_symbol} bars from yfinance …"):
            try:
                _n = DataFetcher().refresh_recent(f_symbol, days_back=max(_gap_days, 10))
                query_bars.clear()
                query_indicator_history.clear()
                st.success(f"Fetched/updated {_n} daily bar(s) for {f_symbol}.")
            except Exception as exc:                       # noqa: BLE001 - surface to UI
                st.error(f"Fetch failed for {f_symbol}: {exc}")

# ── Resolve UTC-naive entry/exit timestamps ───────────────────────────────────
# trades_df timestamps are shifted to local for display; the bar / signal_log
# indexes are UTC-naive.  Re-fetch the raw (unconverted) row and match on a
# stable composite key so the forensic joins align to the right bars.
_raw = get_trade_log(source=source_filter if source_filter else None)
raw_entry_ts = pd.Timestamp(sel["entry_ts"])     # fallback: local (close enough at day granularity)
raw_exit_ts  = pd.Timestamp(sel["exit_ts"])
if not _raw.empty:
    _key = (
        (_raw["symbol"] == sel["symbol"])
        & (_raw["source"] == sel["source"])
        & (_raw["run_id"].astype("string").fillna("") == str(sel["run_id"] or ""))
        & (_raw["shares"] == sel["shares"])
        & (_raw["entry_px"].round(6) == round(float(sel["entry_px"]), 6))
        & (_raw["exit_px"].round(6) == round(float(sel["exit_px"]), 6))
        & (_raw["exit_reason"] == sel["exit_reason"])
    )
    _match = _raw[_key]
    if not _match.empty:
        raw_entry_ts = pd.Timestamp(_match.iloc[0]["entry_ts"])
        raw_exit_ts  = pd.Timestamp(_match.iloc[0]["exit_ts"])

forensics = query_trade_forensics(
    symbol=sel["symbol"],
    signal=sel["signal"],
    entry_ts=raw_entry_ts,
    exit_ts=raw_exit_ts,
    run_id=str(sel["run_id"]) if pd.notna(sel["run_id"]) else "",
    through_today=through_today,
)

price        = forensics["price"]
signals_df   = forensics["signals"]
entry_signal = forensics["entry_signal"]
entry_ind    = forensics["entry_ind"]
bracket      = forensics["bracket"]
benchmark    = forensics["benchmark"]
bench_sym    = forensics["benchmark_symbol"]

entry_px = float(sel["entry_px"])
exit_px  = float(sel["exit_px"])
is_buy   = str(sel["signal"]).upper() == "BUY"

# ── Effective (regime-adjusted) gate threshold the entry signal had to clear ──
_regime = entry_signal["regime"] if entry_signal else None
_base_thr = _app_config.ml.signal_threshold
if _regime == "HIGH_VOLATILITY":
    _eff_thr = _base_thr * _app_config.ml.high_vol_threshold_multiplier
elif _regime == "TRENDING":
    _eff_thr = _base_thr * _app_config.ml.trending_threshold_multiplier
else:
    _eff_thr = _base_thr

# ── Block A — summary strip ───────────────────────────────────────────────────
a1, a2, a3, a4 = st.columns(4)
a1.metric("Direction", str(sel["signal"]))
a1.caption(f"Source: `{sel['source']}`")
a2.metric("Entry", f"${entry_px:,.2f}", help=str(pd.Timestamp(sel['entry_ts']).date()))
a3.metric("Exit",  f"${exit_px:,.2f}",  help=str(pd.Timestamp(sel['exit_ts']).date()))
a4.metric("Holding (days)", f"{int(sel['holding_days'])}")

b1, b2, b3, b4 = st.columns(4)
b1.metric("Net P&L", f"${sel['net_pnl']:,.2f}")
b2.metric("Return", f"{(sel['pnl_pct'] or 0.0) * 100:+.2f}%")
_bench = sel.get("benchmark_return_pct")
if pd.notna(_bench):
    b3.metric(f"Excess vs {bench_sym}",
              f"{(sel['pnl_pct'] or 0.0) * 100 - _bench * 100:+.2f}%")
else:
    b3.metric(f"Excess vs {bench_sym}", "—",
              help="No benchmark bar on entry/exit for this trade.")
b4.metric("Exit reason", str(sel["exit_reason"]))

# ── Block B — hold-trajectory chart (price + bracket + benchmark / scores) ────
if price.empty:
    st.info("No cached OHLCV bars for this window — cannot draw the trajectory "
            "chart.  Run `scripts/run_pipeline.py` to backfill bars for this symbol.")
else:
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.62, 0.38], vertical_spacing=0.06,
        subplot_titles=("Price · bracket levels · benchmark", "Model scores"),
    )

    fig.add_trace(go.Candlestick(
        x=price.index, open=price["Open"], high=price["High"],
        low=price["Low"], close=price["Close"], name="Price",
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
        showlegend=False,
    ), row=1, col=1)

    # Entry / exit markers.
    fig.add_trace(go.Scatter(
        x=[raw_entry_ts], y=[entry_px], mode="markers", name="Entry",
        marker=dict(symbol="triangle-up" if is_buy else "triangle-down",
                    size=14, color="#26a69a", line=dict(width=1, color="white")),
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=[raw_exit_ts], y=[exit_px], mode="markers", name="Exit",
        marker=dict(symbol="x", size=13, color="#ffd54f",
                    line=dict(width=1, color="white")),
    ), row=1, col=1)

    # Bracket levels — authoritative (live order_decisions) or reconstructed.
    _atr = (entry_ind or {}).get("atr_14")
    stop_px = tp_px = None
    bracket_src = None
    if bracket and bracket.get("stop_price"):
        stop_px = bracket.get("stop_price")
        tp_px   = bracket.get("take_profit_price")
        bracket_src = "live order_decisions"
    elif _atr:
        sgn = 1.0 if is_buy else -1.0
        stop_px = entry_px - sgn * _atr * _app_config.risk.atr_stop_multiplier
        tp_px   = entry_px + sgn * _atr * _app_config.risk.atr_take_profit_multiplier
        bracket_src = "reconstructed from ATR×config"
    if stop_px:
        fig.add_hline(y=stop_px, line_dash="dot", line_color="#ef5350",
                      annotation_text="stop", annotation_position="right",
                      row=1, col=1)
    if tp_px:
        fig.add_hline(y=tp_px, line_dash="dot", line_color="#26a69a",
                      annotation_text="TP", annotation_position="right",
                      row=1, col=1)

    # Benchmark, normalised to the entry price (#5).
    if not benchmark.empty:
        b_close = benchmark["Close"]
        b_before = b_close[b_close.index <= raw_entry_ts]
        base_b = b_before.iloc[-1] if not b_before.empty else (
            b_close.iloc[0] if not b_close.empty else None)
        if base_b:
            fig.add_trace(go.Scatter(
                x=b_close.index, y=entry_px * (b_close / base_b),
                mode="lines", name=f"{bench_sym} (norm)",
                line=dict(color="#90a4ae", width=1.5, dash="dash"),
            ), row=1, col=1)

    # Lower panel — model scores over the hold.
    if not signals_df.empty:
        s = signals_df.sort_values("Date")
        for col, color, width in (
            ("Ensemble Score", "#ffffff", 2.5),
            ("LSTM Score",     "#42a5f5", 1.2),
            ("XGB Score",      "#ab47bc", 1.2),
            ("FinBERT Score",  "#ffa726", 1.2),
        ):
            if col in s.columns:
                fig.add_trace(go.Scatter(
                    x=s["Date"], y=s[col], mode="lines", name=col,
                    line=dict(color=color, width=width),
                ), row=2, col=1)
        # Gate thresholds (BUY side +, SELL side −).
        fig.add_hline(y=_eff_thr, line_dash="dash", line_color="#26a69a",
                      row=2, col=1)
        fig.add_hline(y=-_eff_thr, line_dash="dash", line_color="#ef5350",
                      row=2, col=1)
        # Shade HIGH_VOLATILITY spans (contiguous runs).
        if "Regime" in s.columns:
            _hv = (s["Regime"] == "HIGH_VOLATILITY").tolist()
            _dates = s["Date"].tolist()
            i = 0
            while i < len(_hv):
                if _hv[i]:
                    j = i
                    while j + 1 < len(_hv) and _hv[j + 1]:
                        j += 1
                    fig.add_vrect(x0=_dates[i], x1=_dates[j],
                                  fillcolor="#ef5350", opacity=0.07,
                                  line_width=0, row=2, col=1)
                    i = j + 1
                else:
                    i += 1

    fig.update_layout(
        template="plotly_dark", height=620,
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        xaxis_rangeslider_visible=False,
    )
    st.plotly_chart(fig, use_container_width=True)
    _src_note = f"  Bracket levels: {bracket_src}." if bracket_src else \
        "  Bracket levels unavailable (no order_decisions row and no ATR cached)."
    st.caption(
        "Top: price with entry (▲) / exit (✕) markers, the stop/TP bracket, and "
        f"{bench_sym} normalised to the entry price — if the grey line ends above "
        "your exit, a buy-and-hold of the benchmark beat this trade.  Bottom: the "
        "ensemble score (white) and its three components, with the BUY/SELL gate "
        "thresholds (dashed) and HIGH_VOLATILITY regime shaded.  Watch for the "
        "ensemble (or LSTM) rolling over *before* the exit — the model losing "
        f"conviction while the bracket hadn't yet fired.{_src_note}"
    )

# ── Block C — entry decision context + pattern flags ──────────────────────────
st.markdown("##### Entry decision context")
if entry_signal is None:
    st.warning(
        "No `signal_log` row found for this entry.  This happens for "
        "walk-forward trades from before the daily runner logged signals, or "
        "live rows backfilled from a Flex export with no contemporaneous run.  "
        "Entry-context flags need that row, so they're unavailable for this trade."
    )
else:
    es = entry_signal
    ens = es.get("ensemble_score")
    margin = (abs(ens) - _eff_thr) if ens is not None else None

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ensemble", f"{ens:+.3f}" if ens is not None else "—",
              help=f"Effective gate threshold this signal cleared: {_eff_thr:.3f}")
    if margin is not None:
        c1.caption(f"Margin over gate: **{margin:+.3f}**"
                   + ("  ⚠ squeaker" if margin < 0.05 else ""))
    c2.metric("LSTM", f"{es.get('lstm_score'):+.3f}" if es.get('lstm_score') is not None else "—")
    c3.metric("XGB", f"{es.get('xgb_score'):+.3f}" if es.get('xgb_score') is not None else "—")
    c4.metric("FinBERT", f"{es.get('finbert_score'):+.3f}" if es.get('finbert_score') is not None else "—")

    st.caption(
        f"Regime at entry: **{_regime or '—'}**  ·  "
        f"signal bar: {pd.Timestamp(es['bar_timestamp']).date()}  ·  "
        f"gate: {es.get('gate_reason') or '—'}"
    )

    # Exit attribution — be honest about agency.
    _xr = str(sel["exit_reason"])
    if _xr == "signal_flip":
        st.markdown("**Exit:** a model decision — a SELL signal flipped the position out.")
    elif _xr == "fold_end":
        st.markdown("**Exit:** `fold_end` — a walk-forward boundary flatten, not a "
                    "decision the live system would make (backtest artifact).")
    else:
        st.markdown(f"**Exit:** `{_xr}` — the bracket fired automatically; the model "
                    "made no exit decision at the fill instant.  The score panel above "
                    "shows what it was saying as the position was held.")

    # Pattern flags (declarative registry — models/trade_patterns.py).
    ind = entry_ind or {}
    ctx = EntryContext(
        direction=str(sel["signal"]),
        lstm=es.get("lstm_score"), xgb=es.get("xgb_score"),
        finbert=es.get("finbert_score"), ensemble=ens,
        threshold=_eff_thr, regime=_regime,
        rsi=ind.get("rsi_14"), macd=ind.get("macd"),
        bb_upper=ind.get("bb_upper"), bb_lower=ind.get("bb_lower"),
        close=entry_px,
    )
    fired = evaluate_patterns(ctx)
    st.markdown("###### Pattern flags")
    if not fired:
        st.success("No entry-pattern flags fired — no known disagreement / "
                   "over-extension / low-conviction pattern at entry.")
    else:
        _sev_icon = {SEVERITY_WARNING: "🔴", SEVERITY_CAUTION: "🟠"}
        for bucket, plist in group_by_bucket(fired).items():
            st.markdown(f"**{bucket}**")
            for p in plist:
                icon = _sev_icon.get(p.severity, "🔵")
                st.markdown(f"- {icon} **{p.label}** — {p.explain(ctx)}")
    if not entry_ind:
        st.caption("⚠ No indicator snapshot cached near entry — RSI / MACD / "
                   "Bollinger-based flags could not be evaluated for this trade.")

# ── Block D — counterfactual: what happened after the exit ────────────────────
st.markdown("##### Counterfactual — left on the table")
include_exit_bar = st.checkbox(
    "Include the exit-day bar",
    value=False,
    key="forensic_include_exit_bar",
    help="On: measure from the exit *calendar day* onward — captures a "
         "gap-up-open TP's same-day tail (the case-study capture frame).  "
         "Off: only bars strictly after the exit instant ('was there more if I'd "
         "held longer?').  Only changes the numbers when the exit-day bar is the "
         "window's high/low; otherwise it just adds one bar to the count.",
)

# Compute the post-exit window locally so the toggle responds on the same rerun.
_POST_BARS = 10
if price.empty:
    post_exit = price
elif include_exit_bar:
    post_exit = price[price.index.normalize() >= raw_exit_ts.normalize()].iloc[:_POST_BARS]
else:
    post_exit = price[price.index > raw_exit_ts].iloc[:_POST_BARS]

if post_exit.empty:
    st.info("No post-exit bars cached yet (or the trade exited on the most recent "
            "bar) — the left-on-the-table measure needs bars after the exit.")
else:
    n_bars = len(post_exit)
    if is_buy:
        extreme = float(post_exit["High"].max())
        beyond = extreme / exit_px - 1.0
        denom = extreme - entry_px
        captured = (exit_px - entry_px) / denom if denom > 0 else 1.0
        label = "Post-exit peak"
    else:
        extreme = float(post_exit["Low"].min())
        beyond = exit_px / extreme - 1.0
        denom = entry_px - extreme
        captured = (entry_px - exit_px) / denom if denom > 0 else 1.0
        label = "Post-exit trough"

    _frame = "from the exit day onward" if include_exit_bar else "strictly after exit"
    d1, d2, d3 = st.columns(3)
    d1.metric(f"{label} ({n_bars} bars)", f"${extreme:,.2f}")
    d2.metric("Move beyond exit", f"{beyond * 100:+.1f}%")
    d3.metric("Captured of full move", f"{max(0.0, min(captured, 1.0)) * 100:.0f}%")
    st.caption(
        f"Over the {n_bars} bar(s) {_frame}, the {'high' if is_buy else 'low'} "
        f"reached ${extreme:,.2f} ({beyond * 100:+.1f}% past the exit).  'Captured' "
        "is the fraction of the entry→extreme move this trade actually realised — a "
        "low number on a winner is the tp-concentration / trailing-crowded-out "
        "pattern (locked a small win, left a big tail).  Toggle **include the "
        "exit-day bar** above to count a gap-up-open TP's same-day tail (the "
        "case-study capture frame) vs. only what was left if you'd held longer.  "
        "Long-only today, so BUY trades dominate."
    )
