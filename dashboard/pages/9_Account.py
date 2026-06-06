"""
Account & Portfolio — Page 9
==============================
Shows live IBKR account summary, open positions (enriched with live prices
via yfinance and risk levels from `order_decisions`), and open orders.

Connects to IBKR on demand when the user clicks Refresh — no persistent
connection is held between renders.  If IBKR is not running the page
shows the connection error and waits for the next refresh.
"""

from __future__ import annotations

from pathlib import Path
import sys
import asyncio
import concurrent.futures
from datetime import datetime, timezone

_root = Path(__file__).resolve()
while not (_root / "config" / "settings.py").exists() and _root.parent != _root:
    _root = _root.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

# eventkit (ib_insync dependency) calls asyncio.get_event_loop() at import
# time, which raises on Python 3.10+ in Streamlit's ScriptRunner thread.
asyncio.set_event_loop(asyncio.new_event_loop())

from config.settings import config, TradingMode
from data.database import get_latest_risk_levels
from data.ui_queries import query_company_name, query_trade_summary
from risk.portfolio_guard import get_sector

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Account",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Lazy IBKR import (inside function to keep the event-loop fix scoped) ──────

def _import_ibkr():
    from execution.ibkr_connection import AccountSummary, IBKRConnection
    return AccountSummary, IBKRConnection


# ── Async helpers ─────────────────────────────────────────────────────────────

def _run_async(coro):
    """Run an async coroutine from synchronous Streamlit code."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


async def _fetch_ibkr_data() -> dict:
    """Open a short-lived IBKR connection, pull all account data, disconnect."""
    _, IBKRConnection = _import_ibkr()
    async with IBKRConnection() as conn:
        summary   = await conn.get_account_summary()
        positions = await conn.get_positions()
        orders    = await conn.get_open_orders()
    return {
        "summary":    summary,
        "positions":  positions,
        "orders":     orders,
        "fetched_at": datetime.now(timezone.utc),
    }


def _card_metric_html(
    label: str,
    value: str,
    delta: str | None = None,
    mode: str = "normal",
) -> str:
    """
    Render a metric-style block with a smaller value font than st.metric.
    Mimics st.metric's visual layout (label / value / delta) but gives us
    direct control over font sizes, dodging Streamlit CSS specificity.

    mode='normal' colours delta teal (+) or red (-); mode='off' keeps it muted.
    """
    delta_html = ""
    if delta:
        stripped = delta.lstrip()
        if mode == "normal" and stripped.startswith("+"):
            color, arrow = "#26a69a", "▲ "
        elif mode == "normal" and stripped.startswith("-"):
            color, arrow = "#ef5350", "▼ "
        else:
            color, arrow = "rgba(250,250,250,0.6)", ""
        delta_html = (
            f'<div style="font-size: 1.1rem; color: {color}; '
            f'line-height: 1.3;">{arrow}{delta}</div>'
        )
    return (
        '<div style="margin-bottom: 0.75rem;">'
        f'<div style="font-size: 0.95rem; color: rgba(250,250,250,0.6); '
        f'line-height: 1.3; margin-bottom: 0.1rem;">{label}</div>'
        f'<div style="font-size: 1.25rem; font-weight: 400; line-height: 1.3; '
        f'margin-bottom: 0.1rem;">{value}</div>'
        f'{delta_html}'
        '</div>'
    )


def _enrich_positions(
    positions: list[dict],
    risk_levels: dict[str, dict] | None = None,
    open_orders: list[dict] | None = None,
) -> pd.DataFrame:
    """
    Enrich positions with live price data (via yfinance), risk levels from
    `order_decisions`, and live order state from IBKR.

    Cross-referencing matters when a bracket has been mutated — e.g. once
    `TrailingStopManager` converts a bracket TP → standalone TRAIL, the
    original LMT and STP legs are cancelled at IBKR.  Without the cross-ref
    the page would render the cancelled prices as if they were live, which
    is what surfaced as the stale `Take Profit $159.44 (-6.0% away)` card
    on SNOW (2026-05-19): the position had converted to TRAIL on 2026-05-07
    so the original bracket no longer existed.

    Resolution rules per symbol (against open_orders filtered to action='SELL'):
      * Active SELL LMT  → bracket TP still live   → keep `take_profit`
      * Active SELL STP/STP LMT → bracket stop still live → keep `stop_loss`
      * No matching leg  → blank that column (rendered as "—" downstream)
      * Active SELL TRAIL → surface `trail_amount` (= auxPrice = trail
                            distance) and best-effort `trail_trigger`
                            (= trailStopPrice = current ratcheting trigger,
                            None until IBKR sends the first update)

    Adds columns:
      current_price, market_value, unrealized_pnl, pnl_pct,
      entry_limit, stop_loss, take_profit,
      stop_dist_pct (% above stop — lower = closer to being stopped out),
      tp_dist_pct   (% below take-profit — lower = closer to target),
      trail_amount, trail_trigger, trail_dist_pct.
    """
    if not positions:
        return pd.DataFrame()

    df = pd.DataFrame(positions)

    # ── Live prices via yfinance ──────────────────────────────────────────────
    prices: dict[str, float] = {}
    for symbol in df["symbol"].unique():
        try:
            hist = yf.Ticker(symbol).history(period="1d")
            prices[symbol] = float(hist["Close"].iloc[-1]) if not hist.empty else None
        except Exception:
            prices[symbol] = None

    df["current_price"] = df["symbol"].map(prices)
    df["market_value"] = df.apply(
        lambda r: r["quantity"] * r["current_price"]
        if r["current_price"] is not None
        else r["quantity"] * r["avg_cost"],
        axis=1,
    )
    df["unrealized_pnl"] = df["market_value"] - df["quantity"] * df["avg_cost"]
    df["pnl_pct"] = df.apply(
        lambda r: (r["unrealized_pnl"] / (r["quantity"] * r["avg_cost"]) * 100)
        if r["avg_cost"] > 0 else 0,
        axis=1,
    )

    # ── Risk levels from order_decisions (the original bracket prices) ───────
    rl = risk_levels or {}

    def _rl(sym: str, key: str):
        return rl.get(sym, {}).get(key)

    df["entry_limit"] = df["symbol"].apply(lambda s: _rl(s, "entry_price"))
    df["stop_loss"]   = df["symbol"].apply(lambda s: _rl(s, "stop_price"))
    df["take_profit"] = df["symbol"].apply(lambda s: _rl(s, "take_profit_price"))

    # ── Cross-reference live open orders ─────────────────────────────────────
    # Index orders by symbol so we can check each position cheaply.
    orders_by_sym: dict[str, list[dict]] = {}
    for o in (open_orders or []):
        if o.get("action") != "SELL":
            continue
        orders_by_sym.setdefault(o["symbol"], []).append(o)

    def _has_active_tp(sym: str) -> bool:
        return any(o.get("order_type") == "LMT" for o in orders_by_sym.get(sym, []))

    def _has_active_stop(sym: str) -> bool:
        return any(
            o.get("order_type") in ("STP", "STP LMT")
            for o in orders_by_sym.get(sym, [])
        )

    def _trail_amount(sym: str) -> float | None:
        for o in orders_by_sym.get(sym, []):
            if o.get("order_type") == "TRAIL":
                v = o.get("stop_price")  # auxPrice = trail distance
                return float(v) if v is not None else None
        return None

    def _trail_trigger(sym: str) -> float | None:
        for o in orders_by_sym.get(sym, []):
            if o.get("order_type") == "TRAIL":
                v = o.get("trail_stop_price")
                return float(v) if v is not None else None
        return None

    # Blank stop/TP for positions whose bracket leg no longer exists at IBKR.
    # When `open_orders` is None we have no information to disprove the leg —
    # leave the prices as-is for backwards compatibility (e.g. tests that
    # don't supply orders, or a transient IBKR fetch failure).
    if open_orders is not None:
        df.loc[~df["symbol"].apply(_has_active_tp),   "take_profit"] = None
        df.loc[~df["symbol"].apply(_has_active_stop), "stop_loss"]   = None

    df["trail_amount"]  = df["symbol"].apply(_trail_amount)
    df["trail_trigger"] = df["symbol"].apply(_trail_trigger)

    # Distance from current price to stop / TP (long positions)
    # stop_dist_pct > 0  → price is above stop (safe)
    # tp_dist_pct   > 0  → price is below TP  (still room to run)
    def _stop_dist(row):
        cp, sl = row["current_price"], row["stop_loss"]
        if cp and sl and sl > 0:
            return (cp - sl) / cp * 100
        return None

    def _tp_dist(row):
        cp, tp = row["current_price"], row["take_profit"]
        if cp and tp and tp > 0:
            return (tp - cp) / cp * 100
        return None

    def _trail_dist(row):
        cp, tt = row["current_price"], row["trail_trigger"]
        if cp and tt and tt > 0:
            return (cp - tt) / cp * 100
        return None

    df["stop_dist_pct"]  = df.apply(_stop_dist,  axis=1)
    df["tp_dist_pct"]    = df.apply(_tp_dist,    axis=1)
    df["trail_dist_pct"] = df.apply(_trail_dist, axis=1)

    return df[[
        "symbol", "quantity", "avg_cost",
        "entry_limit", "stop_loss", "take_profit",
        "trail_amount", "trail_trigger",
        "current_price", "market_value",
        "unrealized_pnl", "pnl_pct",
        "stop_dist_pct", "tp_dist_pct", "trail_dist_pct",
    ]]


# ── Session state ─────────────────────────────────────────────────────────────

if "ibkr_data"  not in st.session_state:
    st.session_state.ibkr_data  = None
if "ibkr_error" not in st.session_state:
    st.session_state.ibkr_error = None

# ── Sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("💼 Account & Portfolio")
st.sidebar.caption(
    f"Mode: **{config.trading.mode.value.upper()}**  \n"
    f"Host: `{config.ibkr.host}:{config.ibkr.paper_port if config.trading.mode == TradingMode.SIMULATION else config.ibkr.live_port}`"
)
st.sidebar.markdown("---")

refresh = st.sidebar.button("Refresh from IBKR", type="primary", key="acc_refresh")

# ── Header ────────────────────────────────────────────────────────────────────

st.title("Account & Portfolio")

mode        = config.trading.mode
mode_colour = "red" if mode == TradingMode.LIVE else "orange"
st.markdown(
    f"Mode: :{mode_colour}[**{mode.value.upper()}**]  &nbsp;|&nbsp; "
    f"Host: `{config.ibkr.host}:{config.ibkr.paper_port if mode == TradingMode.SIMULATION else config.ibkr.live_port}`"
)

# ── IBKR refresh ──────────────────────────────────────────────────────────────

if refresh:
    with st.spinner("Connecting to IBKR and fetching account data …"):
        try:
            st.session_state.ibkr_data  = _run_async(_fetch_ibkr_data())
            st.session_state.ibkr_error = None
        except Exception as exc:
            st.session_state.ibkr_error = str(exc)
            st.session_state.ibkr_data  = None

# ── Connection status ─────────────────────────────────────────────────────────

if st.session_state.ibkr_data:
    _raw_fetched = st.session_state.ibkr_data["fetched_at"]
    _local_tz    = datetime.now(timezone.utc).astimezone().tzinfo
    if _raw_fetched.tzinfo is None:
        _raw_fetched = _raw_fetched.replace(tzinfo=timezone.utc)
    fetched_at = _raw_fetched.astimezone(_local_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    st.success(f"Connected  |  Last updated: {fetched_at}")
elif st.session_state.ibkr_error:
    st.warning(
        f"**IBKR not connected** — {st.session_state.ibkr_error}  \n"
        "IBKR must be running and API access enabled.  "
        "Signal history and analytics below are still available."
    )
else:
    st.info("Click **Refresh from IBKR** in the sidebar to load live account data.  "
            "IBKR must be running.")

# ═══════════════════════════════════════════════════════════════════════════════
# LIVE IBKR SECTIONS (only when connected)
# ═══════════════════════════════════════════════════════════════════════════════

if st.session_state.ibkr_data:
    data = st.session_state.ibkr_data
    AccountSummary, _ = _import_ibkr()
    summary: AccountSummary = data["summary"]

    # ── Account summary ───────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Account Summary")

    # Row 1 — balance state
    b1, b2, b3 = st.columns(3)
    b1.metric("Net Liquidation",       f"${summary.net_liquidation:,.2f}")
    b2.metric("Total Cash",            f"${summary.total_cash:,.2f}")
    b3.metric("Total Position Value",  f"${summary.gross_position_value:,.2f}")

    # Row 2 — P&L breakdown
    # Lifetime realized P&L is summed from every closed live trade in trade_log
    # (Phase B fills + the one-time Flex backfill).  active_universe_only=False
    # and dedup_to_latest_run=False so no real broker trade is ever dropped —
    # live rows pass both filters untouched, but the explicit flags document the
    # intent (a closed trade can't vanish because the universe rotated).
    live_summary = query_trade_summary(
        source="live",
        active_universe_only=False,
        dedup_to_latest_run=False,
    )
    lifetime_net   = live_summary["net_pnl"]
    lifetime_count = live_summary["n_trades"]

    p1, p2, p3 = st.columns(3)
    p1.metric("Unrealized P&L",        f"${summary.unrealized_pnl:+,.2f}")
    p2.metric("Realized P&L (today)",  f"${summary.realized_pnl:+,.2f}")
    if lifetime_count:
        p3.metric(
            "Realized P&L (lifetime)",
            f"${lifetime_net:+,.2f}",
            help=(
                f"Cumulative realized P&L (net of costs) across {lifetime_count} "
                "closed live trade(s) in `trade_log` (`source='live'`), populated "
                "by Phase 4.5 Phase B reconciliation and the one-time Flex backfill. "
                "See Page 10 for the per-trade breakdown."
            ),
        )
    else:
        p3.metric(
            "Realized P&L (lifetime)",
            "— no live trades yet",
            help=(
                "Cumulative realized P&L across all closed live trades. "
                "No `source='live'` rows in `trade_log` yet — the first reconciled "
                "round trip will populate this."
            ),
        )

    st.caption(
        f"Account ID: {summary.account_id}  |  Currency: {summary.currency}  |  "
        "**Unrealized P&L** is cumulative across all open positions (current price vs avg cost), not a today-only figure. "
        "**Realized P&L (today)** is the intraday figure from IBKR — it resets at session start. "
        "**Realized P&L (lifetime)** is summed net-of-costs from every closed live trade in `trade_log` "
        "(Phase B reconciliation + Flex backfill); IBKR's real-time API doesn't expose a cumulative figure. "
        "See Page 10 for the full trade history."
    )

    # ── Open positions ────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Open Positions")

    positions_raw: list[dict] = data["positions"]
    if not positions_raw:
        st.info("No open positions.")
    else:
        symbols = [p["symbol"] for p in positions_raw]
        risk_levels = get_latest_risk_levels(symbols)
        pos_df = _enrich_positions(positions_raw, risk_levels, data["orders"])

        # ── Risk-level summary cards ──────────────────────────────────────────
        # One card per position, chunked into rows of CARDS_PER_ROW.  Each card:
        #   • Current price + % vs avg cost (positive = up on the position)
        #   • Stop Loss + % above stop  (hidden once the bracket STP has been
        #     cancelled — e.g. trailing-stop conversion)
        #   • Take Profit + % remaining (hidden once the bracket LMT is gone)
        #   • Trailing Stop + trail distance + live trigger
        #     (shown when a SELL TRAIL order is active for this symbol)
        CARDS_PER_ROW = 4
        has_risk_or_price = (
            pos_df["stop_loss"].notna().any()
            or pos_df["current_price"].notna().any()
            or pos_df["trail_amount"].notna().any()
        )
        if has_risk_or_price:
            rows = [
                pos_df.iloc[i:i + CARDS_PER_ROW]
                for i in range(0, len(pos_df), CARDS_PER_ROW)
            ]
            for chunk in rows:
                rm_cols = st.columns(CARDS_PER_ROW)
                for col, (_, row) in zip(rm_cols, chunk.iterrows()):
                    sym = row["symbol"]
                    cp  = row["current_price"]
                    ac  = row["avg_cost"]
                    pp  = row["pnl_pct"]
                    sl  = row["stop_loss"]
                    tp  = row["take_profit"]
                    sd  = row["stop_dist_pct"]
                    td  = row["tp_dist_pct"]
                    ta  = row["trail_amount"]
                    tt  = row["trail_trigger"]
                    tdist = row["trail_dist_pct"]
                    with col.container(border=True):
                        company = query_company_name(sym)
                        company_html = (
                            f'<div style="font-size: 0.85rem; '
                            f'color: rgba(250,250,250,0.6); line-height: 1.2; '
                            f'margin-bottom: 0.4rem;">{company}</div>'
                        ) if company else ""
                        # State badge: every card shows one so heights stay
                        # aligned across the row.  Three states based on the
                        # active SELL legs cross-referenced in _enrich_positions:
                        #   TRAILING — SELL TRAIL alive  (teal — winner;
                        #              conversion only fires after price
                        #              moved past activation_atr × ATR)
                        #   BRACKET  — SELL LMT or STP/STP LMT alive
                        #              (muted slate — standard state)
                        #   MANUAL   — no SELL legs (washed grey — position
                        #              has no broker-side downside protection)
                        if pd.notna(ta):
                            badge_label = "▸ TRAILING"
                            badge_bg    = "#22c55e"   # vivid green — distinct
                                                       # from BRACKET's slate
                            badge_fg    = "white"
                        elif pd.notna(sl) or pd.notna(tp):
                            badge_label = "BRACKET"
                            badge_bg    = "#3b4a5c"   # muted slate
                            badge_fg    = "rgba(250,250,250,0.85)"
                        else:
                            badge_label = "MANUAL"
                            badge_bg    = "rgba(250,250,250,0.10)"
                            badge_fg    = "rgba(250,250,250,0.55)"
                        badge_html = (
                            '<div style="margin-bottom: 0.5rem;">'
                            f'<span style="display: inline-block; padding: 0.1rem 0.5rem; '
                            f'border-radius: 0.4rem; background-color: {badge_bg}; '
                            f'color: {badge_fg}; font-size: 0.7rem; font-weight: 700; '
                            f'letter-spacing: 0.05rem;">{badge_label}</span>'
                            '</div>'
                        )
                        st.markdown(
                            f'<div style="font-size: 1.4rem; font-weight: 700; '
                            f'line-height: 1.3; margin-bottom: 0.1rem;">{sym}</div>'
                            f'{company_html}'
                            f'{badge_html}',
                            unsafe_allow_html=True,
                        )
                        blocks: list[str] = []
                        if pd.notna(cp):
                            blocks.append(_card_metric_html(
                                label=f"Current (avg cost ${ac:,.2f})",
                                value=f"${cp:,.2f}",
                                delta=f"{pp:+.1f}% vs cost" if pd.notna(pp) else None,
                                mode="normal",
                            ))
                        if pd.notna(sl):
                            blocks.append(_card_metric_html(
                                label="Stop Loss",
                                value=f"${sl:,.2f}",
                                delta=f"{sd:+.1f}% above" if pd.notna(sd) else None,
                                mode="normal",
                            ))
                        if pd.notna(tp):
                            blocks.append(_card_metric_html(
                                label="Take Profit",
                                value=f"${tp:,.2f}",
                                delta=f"{td:+.1f}% away" if pd.notna(td) else None,
                                mode="off",
                            ))
                        if pd.notna(ta):
                            # Split the trailing info across two blocks so the
                            # card has the same vertical footprint as a normal
                            # 3-block card (Current + Stop + TP).  Without the
                            # split, trailing cards rendered ~1 block shorter
                            # and broke row alignment.
                            #
                            # Block A — Trailing Stop trigger:
                            #   Live trigger is only present after IBKR sends
                            #   the first ratchet update; until then, render a
                            #   "pending" placeholder rather than a misleading
                            #   $0.00.
                            if pd.notna(tt):
                                trig_value = f"${tt:,.2f}"
                                trig_delta = (
                                    f"{tdist:+.1f}% above" if pd.notna(tdist) else None
                                )
                                trig_mode  = "normal"
                            else:
                                trig_value = "pending"
                                trig_delta = "awaiting IBKR update"
                                trig_mode  = "off"
                            blocks.append(_card_metric_html(
                                label="Trailing Stop",
                                value=trig_value,
                                delta=trig_delta,
                                mode=trig_mode,
                            ))
                            # Block B — Trail Distance: replaces the Take
                            # Profit slot since trailing positions have no
                            # upside cap.
                            blocks.append(_card_metric_html(
                                label="Trail Distance",
                                value=f"${ta:,.2f}",
                                delta="no upside cap",
                                mode="off",
                            ))
                        if blocks:
                            st.markdown("".join(blocks), unsafe_allow_html=True)
                        elif pd.isna(sl) and pd.isna(tp) and pd.isna(cp) and pd.isna(ta):
                            st.caption("No price or risk data")

        st.markdown("")

        # ── Positions table ───────────────────────────────────────────────────
        rename = {
            "symbol":        "Symbol",
            "quantity":      "Qty",
            "avg_cost":      "Avg Cost",
            "entry_limit":   "Entry Limit",
            "stop_loss":     "Stop Loss",
            "take_profit":   "Take Profit",
            "trail_amount":  "Trail $",
            "trail_trigger": "Trail Trig",
            "current_price": "Current",
            "market_value":  "Mkt Value",
            "unrealized_pnl":"Unreal. P&L",
            "pnl_pct":       "P&L %",
            "stop_dist_pct": "→ Stop %",
            "tp_dist_pct":   "→ TP %",
        }
        display_df = pos_df.rename(columns=rename).drop(columns=["trail_dist_pct"])

        def _colour_pnl(val) -> str:
            if pd.isna(val):
                return ""
            return "color: #26a69a" if val >= 0 else "color: #ef5350"

        def _colour_stop_dist(val) -> str:
            """Red < 5 %, amber 5–10 %, green otherwise."""
            if pd.isna(val):
                return ""
            if val < 5:
                return "color: #ef5350; font-weight: bold"
            if val < 10:
                return "color: #ff9800"
            return "color: #26a69a"

        fmt = {
            "Qty":         "{:.0f}",
            "Avg Cost":    "${:,.2f}",
            "Entry Limit": "${:,.2f}",
            "Stop Loss":   "${:,.2f}",
            "Take Profit": "${:,.2f}",
            "Trail $":     "${:,.2f}",
            "Trail Trig":  "${:,.2f}",
            "Current":     "${:,.2f}",
            "Mkt Value":   "${:,.2f}",
            "Unreal. P&L": "${:+,.2f}",
            "P&L %":       "{:+.2f}%",
            "→ Stop %":    "{:+.2f}%",
            "→ TP %":      "{:+.2f}%",
        }
        styled = (
            display_df.style
            .format(fmt, na_rep="—")
            .map(_colour_pnl,       subset=["Unreal. P&L", "P&L %"])
            .map(_colour_stop_dist, subset=["→ Stop %"])
        )
        st.dataframe(styled, use_container_width=True)

        st.caption(
            "**Entry Limit / Stop Loss / Take Profit** are the prices set by the signal runner "
            "when the position was opened (from `order_decisions` table) — shown as **—** once "
            "the bracket leg has been cancelled at IBKR (e.g. trailing-stop conversion). "
            "**Trail $** is the trail distance and **Trail Trig** is IBKR's current ratcheting "
            "trigger; both populate once a position converts to a TRAIL order. "
            "**→ Stop %** = how far the current price is above the stop-loss — "
            "red < 5 %, amber 5–10 %, green > 10 %. "
            "**→ TP %** = remaining upside to the take-profit target. "
            "Live prices are fetched from Yahoo Finance."
        )

        # ── Allocation donut ──────────────────────────────────────────────────
        if len(pos_df) > 1:
            import plotly.express as px
            alloc_fig = px.pie(
                pos_df, names="symbol", values="market_value",
                title="Position allocation by market value",
                hole=0.4, template="plotly_dark",
            )
            alloc_fig.update_layout(height=320, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(alloc_fig, use_container_width=True)
            st.caption(
                "Allocation by current market value of each open position.  "
                "A well-diversified portfolio typically keeps any single position below 10–15% of total equity.  "
                "The system enforces a configurable maximum position size limit (Settings → Trading → Max position size)."
            )

        # ── Sector exposure ───────────────────────────────────────────────────
        # Weight each position by market value, group by sector, and compare
        # against the per-sector cap that PortfolioGuard enforces on new trades.
        st.markdown("---")
        st.subheader("Sector Exposure")

        sector_df = pos_df.copy()
        sector_df["sector"] = sector_df["symbol"].apply(get_sector)
        sector_agg = (
            sector_df.groupby("sector", as_index=False)
            .agg(market_value=("market_value", "sum"),
                 symbols=("symbol", lambda s: ", ".join(sorted(s))))
        )

        equity = float(summary.net_liquidation) if summary.net_liquidation else 0.0
        cap_pct = config.risk.max_sector_exposure_pct
        if equity > 0:
            sector_agg["pct_equity"] = sector_agg["market_value"] / equity
        else:
            sector_agg["pct_equity"] = 0.0
        sector_agg = sector_agg.sort_values("market_value", ascending=True)

        col_donut, col_bar = st.columns([1, 1])

        with col_donut:
            import plotly.express as px
            donut = px.pie(
                sector_agg, names="sector", values="market_value",
                title="Allocation by sector",
                hole=0.4, template="plotly_dark",
            )
            donut.update_layout(height=340, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(donut, use_container_width=True)

        with col_bar:
            # Colour each bar red if it breaches the cap, amber if within 5pp,
            # teal otherwise — matches the colour convention on the positions
            # table.
            def _bar_colour(pct: float) -> str:
                if pct > cap_pct:
                    return "#ef5350"
                if pct > cap_pct - 0.05:
                    return "#ff9800"
                return "#26a69a"

            bar_colours = sector_agg["pct_equity"].apply(_bar_colour).tolist()
            bar_fig = go.Figure()
            bar_fig.add_trace(go.Bar(
                y=sector_agg["sector"],
                x=sector_agg["pct_equity"] * 100,
                orientation="h",
                marker_color=bar_colours,
                customdata=sector_agg[["market_value", "symbols"]].values,
                hovertemplate=(
                    "<b>%{y}</b><br>"
                    "%{x:.2f}% of equity<br>"
                    "$%{customdata[0]:,.0f}<br>"
                    "%{customdata[1]}"
                    "<extra></extra>"
                ),
            ))
            bar_fig.add_vline(
                x=cap_pct * 100,
                line_dash="dash", line_color="#ef5350",
                annotation_text=f"Cap {cap_pct:.0%}",
                annotation_position="top right",
            )
            bar_fig.update_layout(
                template="plotly_dark",
                margin=dict(l=0, r=0, t=40, b=0),
                height=max(280, 28 * len(sector_agg) + 80),
                title="Sector exposure vs cap",
                xaxis_title="% of net liquidation",
                yaxis_title="",
                showlegend=False,
            )
            st.plotly_chart(bar_fig, use_container_width=True)

        # Sector summary table
        table = sector_agg.sort_values("market_value", ascending=False).copy()
        table["Market Value"] = table["market_value"].apply(lambda v: f"${v:,.0f}")
        table["% of Equity"]  = table["pct_equity"].apply(lambda p: f"{p:.1%}")
        table["Status"] = table["pct_equity"].apply(
            lambda p: "🔴 Over cap" if p > cap_pct
            else "🟠 Near cap" if p > cap_pct - 0.05
            else "🟢 OK"
        )
        st.dataframe(
            table.rename(columns={"sector": "Sector", "symbols": "Symbols"})
                 [["Sector", "Symbols", "Market Value", "% of Equity", "Status"]],
            use_container_width=True, hide_index=True,
        )

        unknown_held = sector_agg.loc[sector_agg["sector"] == "Unknown", "symbols"]
        unknown_note = ""
        if not unknown_held.empty:
            unknown_note = (
                f"  \n⚠ **Unclassified symbols (Sector = 'Unknown'):** "
                f"{unknown_held.iloc[0]}.  Sector resolves from the hardcoded "
                "`_SECTOR_MAP` (ETFs/fixtures) then the yfinance sector stored "
                "in `fundamental_data` — 'Unknown' means a symbol has no "
                "fundamentals row yet (run `scripts/run_pipeline.py` or "
                "`scripts/backfill_sectors.py`).  PortfolioGuard's sector check "
                "passes these through silently until then."
            )

        st.caption(
            f"**Sector exposure** = sum of position market values per sector, "
            f"divided by net liquidation ({equity:,.0f}). "
            f"PortfolioGuard's check #5 blocks new BUYs that would push a "
            f"sector above **{cap_pct:.0%}** "
            f"(`config.risk.max_sector_exposure_pct`). "
            f"🔴 over cap · 🟠 within 5 pp of cap · 🟢 OK. "
            f"Note the cap only blocks *new* positions — an existing sector "
            f"can drift over the cap if positions appreciate."
            f"{unknown_note}"
        )

    # ── Open orders ───────────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Open Orders")

    orders: list[dict] = data["orders"]
    if not orders:
        st.info("No open orders.")
    else:
        orders_df = pd.DataFrame(orders)

        def _fmt_price(row):
            otype = row.get("order_type", "")
            lmt   = row.get("limit_price")
            stop  = row.get("stop_price")
            # TRAIL orders carry the trailing distance (auxPrice) in stop_price —
            # NOT a stop trigger level.  Render it as "trail $X.XX" so a $4 trail
            # doesn't read as a broken stop at $4.
            if otype == "TRAIL" and stop is not None:
                return f"Trail $ {stop:,.2f}"
            if otype in ("STP", "STP LMT") and stop is not None:
                return f"Stop @ ${stop:,.2f}" + (f" / ${lmt:,.2f}" if lmt else "")
            if lmt is not None:
                return f"${lmt:,.2f}"
            return "MKT"

        orders_df["price"] = orders_df.apply(_fmt_price, axis=1)
        orders_df = orders_df.drop(columns=["limit_price", "stop_price"])
        st.dataframe(
            orders_df.rename(columns={
                "order_id":   "Order ID",
                "symbol":     "Symbol",
                "action":     "Action",
                "quantity":   "Qty",
                "order_type": "Type",
                "price":      "Price",
                "status":     "Status",
                "filled":     "Filled",
                "remaining":  "Remaining",
            }),
            use_container_width=True,
        )

# ── Footer ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.caption(
    "Live prices via Yahoo Finance where IBKR market data subscription is unavailable.  "
    "Unrealized P&L is approximate."
)
