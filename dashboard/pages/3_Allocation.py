"""
Page 3 — Allocation

The risk-premia book at a glance: the target allocation (core + quality-value +
big-bet satellite), the current holdings reconstructed from reconciled fills
(average-cost basis), the target-vs-current drift, and the rebalance history.

Reads SQLite only.  Current weights are shown as a fraction of *invested capital*
(idle cash is live-only and not in the DB) — the authoritative, cash-aware plan
with exact drift + proposed trades comes from `python scripts/rebalance.py`.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data.ui_queries import query_holdings, query_rebalance_log, query_target_allocation

TEAL, RED, GREY = "#26a69a", "#ef5350", "#888"
SLEEVE_LABEL = {"core": "Core ETF", "satellite_qv": "Satellite — quality-value",
                "satellite_bigbet": "Satellite — big-bet"}

st.set_page_config(page_title="Allocation", layout="wide")
st.title("Allocation")
st.caption(
    "Target vs current allocation for the risk-premia book.  Current weights are a "
    "fraction of *invested capital* (idle cash is live-only).  For the live, "
    "cash-aware plan with exact drift + proposed trades, run "
    "`python scripts/rebalance.py`.  Set targets with `python scripts/set_targets.py`."
)

if st.sidebar.button("↻ Refresh cache"):
    query_target_allocation.clear(); query_holdings.clear(); query_rebalance_log.clear()
    st.rerun()

targets = query_target_allocation()
holdings = query_holdings()
rlog = query_rebalance_log()

if targets.empty:
    st.info("No active targets yet.  Run `python scripts/set_targets.py --init-core` "
            "to seed the pinned ETF core, then add satellite names.")
    st.stop()

# ── Assemble the target-vs-current view ───────────────────────────────────────
held = holdings.set_index("symbol") if not holdings.empty else pd.DataFrame()
invested = float(holdings["market_value"].dropna().sum()) if not holdings.empty else 0.0

def _mv(sym):
    return float(held.loc[sym, "market_value"]) if (sym in held.index and pd.notna(held.loc[sym, "market_value"])) else 0.0

rows = []
for _, t in targets.iterrows():
    mv = _mv(t["ticker"])
    cur_wt = mv / invested if invested else 0.0
    rows.append({"ticker": t["ticker"], "sleeve": t["sleeve"], "label": t.get("label") or "",
                 "target_wt": t["target_weight"], "current_wt": cur_wt,
                 "market_value": mv, "drift_pp": (cur_wt - t["target_weight"]) * 100})
target_tickers = set(targets["ticker"])
untracked = [s for s in (held.index if not held.empty else []) if s not in target_tickers]
for sym in untracked:
    mv = _mv(sym)
    rows.append({"ticker": sym, "sleeve": "untracked", "label": "not in target allocation",
                 "target_wt": 0.0, "current_wt": (mv / invested if invested else 0.0),
                 "market_value": mv, "drift_pp": (mv / invested * 100 if invested else 0.0)})
view = pd.DataFrame(rows)

# ── Top metrics ───────────────────────────────────────────────────────────────
from data.database import compute_holdings_from_fills
realized = sum(v["realized_pnl"] for v in compute_holdings_from_fills().values())
unreal = float(holdings["unrealized_pnl"].dropna().sum()) if not holdings.empty else 0.0
c1, c2, c3, c4 = st.columns(4)
c1.metric("Invested (market value)", f"${invested:,.0f}")
c2.metric("Unrealized P&L", f"${unreal:,.0f}")
c3.metric("Realized P&L (lifetime)", f"${realized:,.0f}")
c4.metric("Open positions", f"{0 if holdings.empty else len(holdings)}")

# ── Target vs current bar ─────────────────────────────────────────────────────
st.subheader("Target vs current weight")
plot = view.sort_values(["sleeve", "target_wt"], ascending=[True, False])
fig = go.Figure()
fig.add_bar(name="Target", x=plot["ticker"], y=plot["target_wt"] * 100, marker_color=GREY)
fig.add_bar(name="Current", x=plot["ticker"], y=plot["current_wt"] * 100, marker_color=TEAL)
fig.update_layout(template="plotly_dark", barmode="group", height=380,
                  margin=dict(l=0, r=0, t=40, b=0), yaxis_title="% of invested capital",
                  legend=dict(orientation="h", y=1.1))
st.plotly_chart(fig, use_container_width=True)
st.caption("Grey = target weight, teal = current (of invested capital).  Tickers at "
           "0% current are not yet held; `untracked` names are positions to sell into "
           "the target allocation.  The cash-aware drift/trade plan is in `rebalance.py`.")

# ── Drift table ───────────────────────────────────────────────────────────────
if untracked:
    st.warning(f"{len(untracked)} untracked holding(s) — not in the target allocation, "
               f"to be liquidated into the targets: {', '.join(untracked)}")

disp = view.copy()
disp["sleeve"] = disp["sleeve"].map(lambda s: SLEEVE_LABEL.get(s, s))
disp["target %"] = (disp["target_wt"] * 100).round(1)
disp["current %"] = (disp["current_wt"] * 100).round(1)
disp["drift pp"] = disp["drift_pp"].round(1)
disp["market value"] = disp["market_value"].round(0)
st.dataframe(
    disp[["ticker", "sleeve", "label", "target %", "current %", "drift pp", "market value"]]
    .style.format({"market value": "${:,.0f}"})
    .background_gradient(subset=["drift pp"], cmap="RdYlGn_r", vmin=-10, vmax=10),
    use_container_width=True, hide_index=True)

# ── Holdings detail ───────────────────────────────────────────────────────────
st.subheader("Holdings (from reconciled fills)")
if holdings.empty:
    st.info("No open positions reconstructed from fill_log.  Fills land here after "
            "`scripts/reconcile_flex.py` / `reconcile_fills.py`.")
else:
    h = holdings.copy()
    st.dataframe(
        h.style.format({"shares": "{:,.4f}", "avg_cost": "${:,.2f}", "price": "${:,.2f}",
                        "market_value": "${:,.0f}", "cost_basis": "${:,.0f}",
                        "unrealized_pnl": "${:,.0f}"}, na_rep="—"),
        use_container_width=True, hide_index=True)
    st.caption("Average-cost basis from fill_log; valued at the latest cached daily close "
               "(run `run_pipeline.py` / `refresh_recent_bars.py` to refresh prices).")

# ── Rebalance history ─────────────────────────────────────────────────────────
st.subheader("Rebalance history")
if rlog.empty:
    st.info("No rebalance runs logged yet.  Live runs (`rebalance.py --no-dry-run`) are "
            "recorded here; dry-runs are not.")
else:
    st.dataframe(rlog.style.format({"nlv": "${:,.0f}", "turnover_pct": "{:.1f}%"}, na_rep="—"),
                 use_container_width=True, hide_index=True)
