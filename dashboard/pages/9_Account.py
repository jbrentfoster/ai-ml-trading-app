"""
Account & Portfolio — Page 9
==============================
Shows live IBKR account summary, open positions (enriched with live prices
via yfinance and risk levels from `order_decisions`), and open orders.

Connects to TWS on demand when the user clicks Refresh — no persistent
connection is held between renders.  If TWS is not running the page
shows the connection error and waits for the next refresh.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

# eventkit (ib_insync dependency) calls asyncio.get_event_loop() at import
# time, which raises on Python 3.10+ in Streamlit's ScriptRunner thread.
asyncio.set_event_loop(asyncio.new_event_loop())

from config.settings import config, TradingMode
from data.database import get_latest_risk_levels
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


def _enrich_positions(
    positions: list[dict],
    risk_levels: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """
    Enrich positions with live price data (via yfinance) and, when available,
    risk levels (entry limit, stop-loss, take-profit) from order_decisions.

    Adds columns:
      current_price, market_value, unrealized_pnl, pnl_pct,
      entry_limit, stop_loss, take_profit,
      stop_dist_pct (% above stop — lower = closer to being stopped out),
      tp_dist_pct   (% below take-profit — lower = closer to target).
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

    # ── Risk levels from order_decisions ─────────────────────────────────────
    rl = risk_levels or {}

    def _rl(sym: str, key: str):
        return rl.get(sym, {}).get(key)

    df["entry_limit"] = df["symbol"].apply(lambda s: _rl(s, "entry_price"))
    df["stop_loss"]   = df["symbol"].apply(lambda s: _rl(s, "stop_price"))
    df["take_profit"] = df["symbol"].apply(lambda s: _rl(s, "take_profit_price"))

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

    df["stop_dist_pct"] = df.apply(_stop_dist, axis=1)
    df["tp_dist_pct"]   = df.apply(_tp_dist,   axis=1)

    return df[[
        "symbol", "quantity", "avg_cost",
        "entry_limit", "stop_loss", "take_profit",
        "current_price", "market_value",
        "unrealized_pnl", "pnl_pct",
        "stop_dist_pct", "tp_dist_pct",
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

refresh = st.sidebar.button("Refresh from TWS", type="primary", key="acc_refresh")

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
    with st.spinner("Connecting to TWS and fetching account data …"):
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
        "TWS must be running and API access enabled.  "
        "Signal history and analytics below are still available."
    )
else:
    st.info("Click **Refresh from TWS** in the sidebar to load live account data.  "
            "TWS must be running.")

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

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Net Liquidation", f"${summary.net_liquidation:,.2f}")
    c2.metric("Total Cash",      f"${summary.total_cash:,.2f}")
    c3.metric("Buying Power",    f"${summary.buying_power:,.2f}")
    c4.metric("Unrealized P&L",  f"${summary.unrealized_pnl:+,.2f}")
    c5.metric("Realized P&L (today)", f"${summary.realized_pnl:+,.2f}")

    st.caption(
        f"Account ID: {summary.account_id}  |  Currency: {summary.currency}  |  "
        "Realized P&L is the intraday figure from IBKR — it resets at session start. "
        "Cumulative realized P&L will be available on the Trade History page once live-fill ingestion (Phase B) lands."
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
        pos_df = _enrich_positions(positions_raw, risk_levels)

        # ── Risk-level summary cards ──────────────────────────────────────────
        # One column per position: Stop Loss and Take Profit as st.metric cards.
        # delta on Stop = % above stop (positive = safe, negative = already below stop).
        # delta on TP   = % remaining to target (positive = room to run).
        has_risk = pos_df["stop_loss"].notna().any()
        if has_risk:
            rm_cols = st.columns(len(pos_df))
            for col, (_, row) in zip(rm_cols, pos_df.iterrows()):
                sym = row["symbol"]
                sl  = row["stop_loss"]
                tp  = row["take_profit"]
                sd  = row["stop_dist_pct"]
                td  = row["tp_dist_pct"]
                with col:
                    st.markdown(f"**{sym}**")
                    if pd.notna(sl):
                        st.metric(
                            label="Stop Loss",
                            value=f"${sl:,.2f}",
                            delta=f"{sd:+.1f}% above" if pd.notna(sd) else None,
                            delta_color="normal",   # green = far above stop = good
                        )
                    if pd.notna(tp):
                        st.metric(
                            label="Take Profit",
                            value=f"${tp:,.2f}",
                            delta=f"{td:+.1f}% away" if pd.notna(td) else None,
                            delta_color="off",
                        )
                    if pd.isna(sl) and pd.isna(tp):
                        st.caption("No risk data")

        st.markdown("")

        # ── Positions table ───────────────────────────────────────────────────
        rename = {
            "symbol":        "Symbol",
            "quantity":      "Qty",
            "avg_cost":      "Avg Cost",
            "entry_limit":   "Entry Limit",
            "stop_loss":     "Stop Loss",
            "take_profit":   "Take Profit",
            "current_price": "Current",
            "market_value":  "Mkt Value",
            "unrealized_pnl":"Unreal. P&L",
            "pnl_pct":       "P&L %",
            "stop_dist_pct": "→ Stop %",
            "tp_dist_pct":   "→ TP %",
        }
        display_df = pos_df.rename(columns=rename)

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
            "when the position was opened (from `order_decisions` table).  "
            "**→ Stop %** = how far the current price is above the stop-loss — "
            "red < 5 %, amber 5–10 %, green > 10 %.  "
            "**→ TP %** = remaining upside to the take-profit target.  "
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
                f"  \n⚠ **Unmapped symbols (Sector = 'Unknown'):** "
                f"{unknown_held.iloc[0]}.  PortfolioGuard's sector check "
                "passes these through silently — add to `_SECTOR_MAP` in "
                "`risk/portfolio_guard.py` to bring them under the cap."
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
