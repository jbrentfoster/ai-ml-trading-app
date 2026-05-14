"""
Page 8 — Risk & Portfolio Management

Sections:
  1. Circuit breaker status banner (green = clear / red = halted)
  2. Daily signal runner log
  3. Order decisions log (color-coded by outcome)
  4. Risk metrics cards + Kelly criterion explainer
  5. Circuit breaker event log
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config.settings import config
from data.ui_queries import (
    query_circuit_breaker_log,
    query_circuit_breaker_status,
    query_distinct_run_ids,
    query_order_decisions,
    query_signal_runner_log,
    query_trailing_stop_log,
    symbol_picker,
)

st.set_page_config(page_title="Risk & Portfolio", layout="wide")
st.title("Risk & Portfolio Management")
st.markdown(
    "Monitor the automated risk layer: circuit breaker state, daily signal runs, "
    "and order decisions.  All trades pass through **PortfolioGuard** before submission."
)

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Controls")

    if st.button("Refresh cache"):
        query_circuit_breaker_status.clear()
        query_circuit_breaker_log.clear()
        query_order_decisions.clear()
        query_signal_runner_log.clear()
        query_trailing_stop_log.clear()
        st.rerun()

    st.divider()

    # Manual circuit breaker controls
    st.subheader("Circuit Breaker")
    st.caption("Override the automated halt state.")

    if st.button("🔴 Trigger halt", type="secondary"):
        from risk.circuit_breaker import CircuitBreaker
        CircuitBreaker().trigger(reason="Manual trigger from dashboard")
        query_circuit_breaker_status.clear()
        st.success("Circuit breaker triggered.")
        st.rerun()

    if st.button("🟢 Reset halt", type="primary"):
        from risk.circuit_breaker import CircuitBreaker
        CircuitBreaker().reset()
        query_circuit_breaker_status.clear()
        st.success("Circuit breaker reset.")
        st.rerun()

    st.divider()

    # Signal runner quick-launch
    st.subheader("Signal Runner")
    st.caption("Run the signal pipeline manually.")
    sym_input = symbol_picker(
        "Symbol (blank = all)",
        default="",
        key="rp_run_symbol",
        sidebar=False,
        help="Pick from the current Stage 3 universe or type any symbol; leave blank to run every symbol.",
    )

    if st.button("Run (dry-run)", use_container_width=True):
        from scripts.signal_runner import run as _run
        with st.spinner("Running signal pipeline…"):
            _run(dry_run=True, symbol_filter=sym_input)
        query_signal_runner_log.clear()
        query_order_decisions.clear()
        st.success("Done — results in the log below.")
        st.rerun()


# ── 1. Circuit breaker status ──────────────────────────────────────────────────

st.header("Circuit Breaker Status")

cb_status = query_circuit_breaker_status()
halted    = cb_status.get("halted", False)

if halted:
    reason = cb_status.get("reason", "Unknown reason")
    triggered_at = cb_status.get("triggered_at")
    st.error(
        f"**🔴 TRADING HALTED** — {reason}"
        + (f"  |  Triggered at {triggered_at}" if triggered_at else ""),
        icon="🛑",
    )
    st.caption(
        "All new signals are blocked until the circuit breaker is reset.  "
        "Use the sidebar control or `CircuitBreaker().reset()` to clear."
    )
else:
    st.success("**🟢 Circuit breaker clear** — trading is enabled.", icon="✅")
    st.caption(
        "The circuit breaker triggers automatically when daily loss exceeds "
        f"{config.risk.circuit_breaker_daily_loss_pct:.0%} or weekly loss exceeds "
        f"{config.risk.circuit_breaker_weekly_loss_pct:.0%}.  "
        f"Auto-reset after {config.risk.circuit_breaker_reset_hours}h."
    )


# ── 2. Signal runner log ───────────────────────────────────────────────────────

st.header("Signal Runner Log")
st.caption(
    "Each row is one `signal_runner.py` run.  Run the pipeline daily (manually or via "
    "scheduler) to populate this table.  **Submitted** = DRY_RUN + APPROVED orders."
)

sr_df = query_signal_runner_log(limit=30)
if sr_df.empty:
    st.info(
        "No signal runner history yet.  Run `python signal_runner.py` from the project "
        "root (or use the sidebar button above) to generate the first entry."
    )
else:
    display_cols = [
        "Date", "Mode", "Symbols", "Signals",
        "Submitted", "Rejected", "Closed", "Skipped", "Trailing",
        "Duration (s)",
    ]
    st.dataframe(
        sr_df[[c for c in display_cols if c in sr_df.columns]],
        use_container_width=True,
        hide_index=True,
    )


# ── 3. Order decisions ─────────────────────────────────────────────────────────

st.header("Order Decisions")
st.caption(
    "Every actionable signal that passes the signal gate generates an order decision.  "
    "**DRY_RUN** = logged without submitting.  **REJECTED** = blocked by PortfolioGuard.  "
    "**APPROVED** = bracket order submitted to IBKR.  "
    "**CLOSED_LONG** = SELL signal closed an existing long position (long-only mode).  "
    "**REJECTED_NO_POSITION** = SELL signal ignored — no long held and short selling is disabled."
)

run_ids = query_distinct_run_ids(limit=20)
if not run_ids:
    st.info(
        "No order decisions logged yet.  Decisions appear after the signal runner "
        "generates at least one BUY or SELL signal that passes the signal gate."
    )
else:
    # Run selector — default to latest
    run_labels = {rid: f"{rid[:8]}…  ({i+1} run{'s' if i else ''} ago)"
                  for i, rid in enumerate(run_ids)}
    run_labels[run_ids[0]] = f"{run_ids[0][:8]}…  (latest)"

    selected_run = st.selectbox(
        "Run",
        options=run_ids,
        format_func=lambda rid: run_labels[rid],
        index=0,
    )

    od_df = query_order_decisions(limit=500, run_id=selected_run)

    if od_df.empty:
        st.info("No decisions found for this run.")
    else:
        # Color-code rows by decision
        def _decision_color(decision: str) -> str:
            return {
                "APPROVED":             "background-color: #1b3a2a",   # dark green
                "DRY_RUN":              "background-color: #1a2b3a",   # dark blue
                "REJECTED":             "background-color: #3a1b1b",   # dark red
                "CLOSED_LONG":          "background-color: #2a1b3a",   # dark purple
                "REJECTED_NO_POSITION": "background-color: #2a2a1b",   # dark amber
            }.get(decision, "")

        display_cols = [
            "Symbol", "Signal", "Decision", "Shares", "Entry", "Stop",
            "Take Profit", "Position $", "Reason", "Decided At",
        ]
        styled = od_df[
            [c for c in display_cols if c in od_df.columns]
        ].style.apply(
            lambda row: [_decision_color(row.get("Decision", ""))] * len(row), axis=1
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # ── Position summary ───────────────────────────────────────────
        st.subheader("Position Summary")

        pos_col = "Position $"
        total_capital  = od_df[pos_col].sum() if pos_col in od_df.columns else 0
        avg_position   = od_df[pos_col].mean() if pos_col in od_df.columns else 0
        n_total        = len(od_df)
        counts         = od_df["Decision"].value_counts()
        sig_counts     = od_df["Signal"].value_counts() if "Signal" in od_df.columns else {}
        n_buy          = sig_counts.get("BUY",  0)
        n_sell         = sig_counts.get("SELL", 0)

        r1c1, r1c2, r1c3, r1c4 = st.columns(4)
        r1c1.metric("Total Positions",       n_total)
        r1c2.metric("Total Capital",         f"${total_capital:,.0f}")
        r1c3.metric("Avg Position Size",     f"${avg_position:,.0f}")
        r1c4.metric("BUY / SELL",            f"{n_buy} / {n_sell}")

        r2c1, r2c2, r2c3, r2c4, r2c5 = st.columns(5)
        r2c1.metric("Approved",      counts.get("APPROVED",             0))
        r2c2.metric("Dry Run",       counts.get("DRY_RUN",              0))
        r2c3.metric("Rejected",      counts.get("REJECTED",             0))
        r2c4.metric("Closed Long",   counts.get("CLOSED_LONG",          0))
        r2c5.metric("No Position",   counts.get("REJECTED_NO_POSITION", 0))

        st.caption(
            f"Total capital committed assumes all {n_total} decisions were filled at entry price. "
            "DRY_RUN orders are simulated — no real capital is deployed."
        )


# ── 3b. Trailing stop log ──────────────────────────────────────────────────────

st.header("Trailing Stop Log")
st.caption(
    "Phase 3.5 runs before order submission when `risk.trailing_stop_enabled=True` and "
    "paper/live orders are enabled.  For each open long, the manager either **CONVERTED** "
    "its bracket TP+stop into a standalone GTC TRAIL order, **SKIPPED** it (below activation "
    "threshold, already trailing, or missing ATR/bracket), or **FAILED** mid-conversion.  "
    "Read this alongside the order-decisions table above to see the full picture of today's "
    "order-book mutations."
)

ts_df = query_trailing_stop_log(limit=200)
if ts_df.empty:
    if not config.risk.trailing_stop_enabled:
        st.info(
            "No trailing stop activity logged yet.  "
            "Trailing stops are currently **disabled** "
            "(`risk.trailing_stop_enabled=False`)."
        )
    else:
        st.info(
            "No trailing stop activity logged yet.  Phase 3.5 runs only when "
            "`paper_orders_enabled=True` (or LIVE mode) and evaluates open long positions "
            "on each signal_runner cycle."
        )
else:
    def _ts_color(action: str) -> str:
        return {
            "CONVERTED": "background-color: #1b3a2a",   # dark green
            "SKIPPED":   "background-color: #2a2a1b",   # dark amber
            "FAILED":    "background-color: #3a1b1b",   # dark red
        }.get(action, "")

    display_cols = [
        "Symbol", "Action", "Shares", "Entry", "Current",
        "ATR", "Trail $", "Reason", "Decided At",
    ]
    styled_ts = ts_df[
        [c for c in display_cols if c in ts_df.columns]
    ].style.apply(
        lambda row: [_ts_color(row.get("Action", ""))] * len(row), axis=1
    ).format({
        "Entry":   "${:,.2f}",
        "Current": "${:,.2f}",
        "ATR":     "${:,.2f}",
        "Trail $": "${:,.2f}",
    }, na_rep="—")
    st.dataframe(styled_ts, use_container_width=True, hide_index=True)


# ── 4. Risk configuration cards ───────────────────────────────────────────────

st.header("Risk Configuration")
st.caption("Current settings from `config/settings.yaml` → `RiskConfig` and `TradingConfig`.")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Cash Reserve",         f"{config.trading.cash_reserve_pct:.0%}")
col2.metric("Max Position Size",    f"{config.trading.max_position_size_pct:.0%}")
col3.metric("Max Portfolio Drawdown", f"{config.trading.max_portfolio_drawdown_pct:.0%}")
col4.metric("Kelly Fraction",       f"{config.risk.kelly_fraction:.0%}")
col5.metric("Kelly Max Position",   f"{config.risk.kelly_max_position_pct:.0%}")

col5, col6, col7, col8 = st.columns(4)
col5.metric("ATR Stop Mult.",       f"{config.risk.atr_stop_multiplier:.1f}×")
col6.metric("ATR TP Mult.",         f"{config.risk.atr_take_profit_multiplier:.1f}×")
col7.metric("Daily CB Trigger",     f"{config.risk.circuit_breaker_daily_loss_pct:.0%}")
col8.metric("Weekly CB Trigger",    f"{config.risk.circuit_breaker_weekly_loss_pct:.0%}")

# Trailing stop settings
st.markdown("##### Trailing Stops")
col9, col10, col11, col12 = st.columns(4)
trail_enabled = config.risk.trailing_stop_enabled
col9.metric(
    "Trailing Enabled",
    "✅ Yes" if trail_enabled else "❌ No",
    help=(
        "When enabled (and paper_orders_enabled=True), winning longs have their "
        "bracket take-profits converted to GTC trailing stops."
    ),
)
col10.metric(
    "Activation",
    f"{config.risk.trailing_stop_activation_atr:.1f}× ATR",
    help="Convert once current_price ≥ entry + N × ATR",
)
col11.metric(
    "Trail Distance",
    f"{config.risk.trailing_stop_trail_atr:.1f}× ATR",
    help="Trailing stop sits this far below the peak price",
)
# At-activation outcome — a user-friendly readout showing what break-even means
# for the chosen numbers (activation − trail, in ATR units).
at_activation = config.risk.trailing_stop_activation_atr - config.risk.trailing_stop_trail_atr
if at_activation >= 0:
    at_activation_label = f"Entry + {at_activation:.1f}× ATR"
else:
    at_activation_label = f"Entry − {abs(at_activation):.1f}× ATR"
col12.metric(
    "Initial Stop (at activation)",
    at_activation_label,
    help=(
        "Where the trailing stop lands the moment it activates, before any price "
        "movement ratchets it higher.  0× = break-even, positive = locked-in profit, "
        "negative = small loss still possible."
    ),
)

with st.expander("Kelly Criterion — how position sizes are calculated"):
    st.markdown("""
### Kelly Criterion

The Kelly criterion finds the theoretically optimal fraction of capital to bet
so that long-run wealth growth is maximised.

**Full Kelly formula:**

```
f* = (p × b − q) / b
```

Where:
- **p** = win rate (fraction of past signals that were profitable)
- **q** = 1 − p (loss rate)
- **b** = average win / average loss (reward-to-risk ratio)

**Fractional Kelly** multiplies `f*` by a conservative fraction (default: 25%,
i.e. *quarter-Kelly*).  This dramatically reduces drawdowns at the cost of a
small reduction in long-run growth — widely recommended for real trading.

**Fallback sizing:** If there is insufficient signal-log history (<
`kelly_min_trades` = {kelly_min_trades}), the system falls back to a
**fixed-stop** size: risk 1% of equity per trade using a {stop_pct:.0%} stop.

**Stops from ATR:** Entry stop is placed at `entry ± ATR × {atr_stop}×`;
take-profit at `entry ± ATR × {atr_tp}×`.  When ATR is zero, a fixed {stop_pct:.0%}
stop is used instead.
""".format(
        kelly_min_trades=config.risk.kelly_min_trades,
        stop_pct=config.risk.fixed_stop_loss_pct,
        atr_stop=config.risk.atr_stop_multiplier,
        atr_tp=config.risk.atr_take_profit_multiplier,
    ))


# ── 5. Circuit breaker event log ──────────────────────────────────────────────

st.header("Circuit Breaker Events")
st.caption("Full history of TRIGGERED / RESET / AUTO_RESET events.")

cb_log = query_circuit_breaker_log(limit=50)
if cb_log.empty:
    st.info("No circuit breaker events logged yet.")
else:
    def _cb_color(event: str) -> str:
        return {
            "TRIGGERED":  "background-color: #3a1b1b",
            "RESET":      "background-color: #1b3a2a",
            "AUTO_RESET": "background-color: #1a2b3a",
        }.get(event, "")

    styled_cb = cb_log.style.apply(
        lambda row: [_cb_color(row.get("Event", ""))] * len(row), axis=1
    )
    st.dataframe(styled_cb, use_container_width=True, hide_index=True)
