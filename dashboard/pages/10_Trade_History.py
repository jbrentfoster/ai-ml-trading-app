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
import streamlit as st

from data.ui_queries import (
    query_distinct_trade_log_run_ids,
    query_tax_breakdown,
    query_trade_log,
    query_trade_log_filter_options,
    query_trade_summary,
)

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

opts = query_trade_log_filter_options()
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

    # Dedup toggle — defaults ON because every weekly --force retrain inserts
    # a fresh batch of WF trades with new run_ids; without dedup the page
    # stacks every historical run on top of itself and inflates summary cards.
    # Off = full multi-run history (useful for auditing model drift over time).
    dedup_to_latest = st.checkbox(
        "Dedupe to latest run per symbol",
        value=True,
        help=(
            "ON (default): for walk_forward rows, keep only the latest training "
            "run per symbol — current model only.  OFF: show every weekly "
            "retrain stacked together (Nx duplicates after N runs).  Has no "
            "effect on live rows or when a specific Run ID is selected below."
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
