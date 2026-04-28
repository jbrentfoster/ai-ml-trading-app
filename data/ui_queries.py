"""
UI query functions for all dashboard pages.

All functions read from SQLite via data/database.py helpers.
@st.cache_data(ttl=300) prevents hammering SQLite on every interaction.
No direct API calls are made here — the UI is read-only against the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st


# ── Timezone helpers ──────────────────────────────────────────────────────────
#
# All datetimes are stored UTC-naive (project convention — see CLAUDE.md).
# Dashboard tables should display wall-clock local time to the user, so every
# query function below passes event-time columns through _to_local_series().
#
# Bar/market-boundary timestamps (bar_timestamp, train/test bounds, latest_*
# bar times) are intentionally left in UTC — they represent a market-data
# boundary, and shifting e.g. a daily bar timestamp back 4 hours would make it
# read as the prior calendar day.

def _to_local_series(series: pd.Series) -> pd.Series:
    """Convert a UTC-naive (or tz-aware) datetime Series to local-naive."""
    if series.empty:
        return series
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    s = pd.to_datetime(series, errors="coerce")
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize("UTC")
    return s.dt.tz_convert(local_tz).dt.tz_localize(None)


def _to_local_dt(dt: datetime | None) -> datetime | None:
    """Convert a UTC-naive datetime to local-naive (returns None unchanged)."""
    if dt is None:
        return None
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz).replace(tzinfo=None)

from data.database import (
    get_bars,
    get_circuit_breaker_log,
    get_engine,
    get_fundamentals,
    get_latest_circuit_breaker_event,
    get_latest_indicators,
    get_order_decisions,
    get_recent_news,
    get_signal_runner_log,
    get_trailing_stop_log,
    get_universe_assets,
    get_universe_run_log,
    EnsembleWeightHistory,
    SignalLog,
    WalkForwardResult,
)
from sqlalchemy import desc
from sqlalchemy.orm import Session


# ── Market data ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_bars(symbol: str, interval: str,
               start_date=None, end_date=None,
               limit: int = 1000) -> pd.DataFrame:
    """
    Fetch OHLCV bars from SQLite, optionally filtered by date range.
    Fetches `limit` most-recent bars, then trims to the requested window.
    """
    df = get_bars(symbol, interval, limit=limit)
    if df.empty:
        return df
    if start_date is not None:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date is not None:
        # Inclusive of the whole day — US daily bars are stamped at 04:00 UTC.
        df = df[df.index < pd.Timestamp(end_date) + pd.Timedelta(days=1)]
    return df


@st.cache_data(ttl=300)
def query_watchlist_summary(watchlist: tuple, interval: str) -> pd.DataFrame:
    """Return one summary row per watchlist symbol."""
    rows = []
    for sym in watchlist:
        sym_df = get_bars(sym, interval, limit=2)
        ind    = get_latest_indicators(sym, interval)

        if sym_df.empty:
            rows.append({"Symbol": sym, "Close": None, "Change%": None,
                         "RSI": None, "MACD": None, "ATR": None, "Updated": None})
            continue

        last = float(sym_df["Close"].iloc[-1])
        prev = float(sym_df["Close"].iloc[-2]) if len(sym_df) > 1 else last
        chg  = (last - prev) / prev * 100 if prev else 0.0

        rows.append({
            "Symbol":  sym,
            "Close":   last,
            "Change%": chg,
            "RSI":     ind["rsi_14"]    if ind else None,
            "MACD":    ind["macd"]      if ind else None,
            "ATR":     ind["atr_14"]    if ind else None,
            "Updated": ind["timestamp"] if ind else None,
        })

    return pd.DataFrame(rows).set_index("Symbol")


# ── Fundamentals & News ───────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_fundamentals(symbol: str) -> dict | None:
    """Return the cached fundamental snapshot for `symbol`, or None."""
    return get_fundamentals(symbol)


@st.cache_data(ttl=300)
def query_news(symbol: str, days_back: int = 30) -> pd.DataFrame:
    """
    Return cached news articles for `symbol` sorted newest-first.
    Adds a `sentiment_label` column: Positive / Negative / Neutral.
    """
    since    = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_back)
    articles = get_recent_news(symbol, since=since)
    if not articles:
        return pd.DataFrame()

    df = pd.DataFrame(articles)
    df["published_at"] = _to_local_series(df["published_at"])
    df = df.sort_values("published_at", ascending=False).reset_index(drop=True)

    def _label(score) -> str:
        if score is None or pd.isna(score):
            return "Neutral"
        return "Positive" if score > 0.1 else ("Negative" if score < -0.1 else "Neutral")

    df["sentiment_label"] = df["sentiment_score"].apply(_label)
    return df


# ── Signal log ────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_signal_log(symbol: str = "",
                     start_date=None, end_date=None,
                     limit: int = 500) -> pd.DataFrame:
    """Return SignalLog rows, optionally filtered by symbol and date range."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(SignalLog)
        if symbol:
            q = q.filter(SignalLog.symbol == symbol)
        if start_date:
            q = q.filter(SignalLog.bar_timestamp >= pd.Timestamp(start_date))
        if end_date:
            # Inclusive of the whole day — bars are stamped at 04:00 UTC for
            # US daily data, so `<= end_date midnight` would clip same-day bars.
            q = q.filter(SignalLog.bar_timestamp < pd.Timestamp(end_date) + pd.Timedelta(days=1))
        rows = q.order_by(desc(SignalLog.bar_timestamp)).limit(limit).all()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "Date":           r.bar_timestamp,       # bar-boundary; keep UTC
        "Symbol":         r.symbol,
        "Signal":         r.signal,
        "Passed Gate":    r.passed_gate,
        "Ensemble Score": r.ensemble_score,
        "LSTM Score":     r.lstm_score,
        "XGB Score":      r.xgb_score,
        "FinBERT Score":  r.finbert_score,
        "Regime":         r.regime,
        "Gate Reason":    r.gate_reason,
        "Generated At":   r.generated_at,
    } for r in rows])
    df["Generated At"] = _to_local_series(df["Generated At"])
    return df


# ── Ensemble weights ──────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_latest_ensemble_weights() -> dict | None:
    """Return the most recent ensemble weight snapshot, or None."""
    engine = get_engine()
    with Session(engine) as session:
        row = (
            session.query(EnsembleWeightHistory)
            .order_by(desc(EnsembleWeightHistory.recorded_at))
            .first()
        )
    if not row:
        return None
    return {
        "lstm":        row.lstm_weight,
        "xgb":         row.xgb_weight,
        "finbert":     row.finbert_weight,
        "recorded_at": _to_local_dt(row.recorded_at),
        "trigger":     row.trigger,
    }


@st.cache_data(ttl=300)
def query_ensemble_weight_history() -> pd.DataFrame:
    """Return the full history of ensemble weight snapshots."""
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(EnsembleWeightHistory)
            .order_by(EnsembleWeightHistory.recorded_at)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "recorded_at": r.recorded_at,
        "LSTM":        r.lstm_weight,
        "XGBoost":     r.xgb_weight,
        "FinBERT":     r.finbert_weight,
        "trigger":     r.trigger,
    } for r in rows])
    df["recorded_at"] = _to_local_series(df["recorded_at"])
    return df


# ── Walk-forward results ──────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_walk_forward_results(symbol: str = "") -> pd.DataFrame:
    """Return WalkForwardResult rows, optionally filtered by symbol."""
    engine = get_engine()
    with Session(engine) as session:
        q = session.query(WalkForwardResult)
        if symbol:
            q = q.filter(WalkForwardResult.symbol == symbol)
        rows = q.order_by(
            WalkForwardResult.recorded_at,
            WalkForwardResult.fold_index,
        ).all()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "Run ID":             r.run_id,
        "Symbol":             r.symbol,
        "Fold":               r.fold_index + 1,
        "Train Start":        r.train_start,       # bar-boundary; keep UTC
        "Train End":          r.train_end,         # bar-boundary; keep UTC
        "Test Start":         r.test_start,        # bar-boundary; keep UTC
        "Test End":           r.test_end,          # bar-boundary; keep UTC
        "Total Return":       r.total_return,
        "Ann. Return":        r.annualized_return,
        "Sharpe Ratio":       r.sharpe_ratio,
        "Max Drawdown":       r.max_drawdown,
        "Win Rate":           r.win_rate,
        "# Signals":          r.n_signals,
        "Sentiment Note":     r.sentiment_note,
        "Recorded At":        r.recorded_at,
    } for r in rows])
    df["Recorded At"] = _to_local_series(df["Recorded At"])
    return df


# ── Universe ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def query_universe_assets(active_only: bool = True) -> pd.DataFrame:
    """
    Return universe_assets rows as a display-ready DataFrame.
    Columns are renamed for human readability.
    """
    df = get_universe_assets(active_only=active_only)
    if df.empty:
        return df
    df = df.copy()
    for col in ("added_at", "last_scored_at", "removed_at"):
        if col in df.columns:
            df[col] = _to_local_series(df[col])
    rename = {
        "symbol":            "Symbol",
        "name":              "Name",
        "asset_class":       "Class",
        "exchange":          "Exchange",
        "is_fixture":        "Fixture",
        "stage":             "Stage",
        "market_cap":        "Market Cap",
        "avg_dollar_volume": "Avg $ Volume",
        "xgb_score":         "XGB Score",
        "active":            "Active",
        "added_at":          "Added",
        "last_scored_at":    "Last Scored",
        "removed_at":        "Removed",
    }
    return df.rename(columns=rename)


@st.cache_data(ttl=300)
def query_universe_run_log(limit: int = 100) -> pd.DataFrame:
    """Return the most recent universe run log entries, newest first."""
    df = get_universe_run_log(limit=limit)
    if df.empty:
        return df
    df = df.copy()
    if "recorded_at" in df.columns:
        df["recorded_at"] = _to_local_series(df["recorded_at"])
    rename = {
        "run_id":           "Run ID",
        "run_type":         "Type",
        "stage":            "Stage",
        "symbol_count":     "Count",
        "duration_seconds": "Duration (s)",
        "recorded_at":      "Recorded At",
        "notes":            "Notes",
    }
    return df.rename(columns=rename).drop(columns=["id"], errors="ignore")


# ── Data status ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def query_data_status() -> pd.DataFrame:
    """
    Return one summary row per currently-tracked symbol.

    Symbol universe = union of:
      - active symbols in universe_assets (if config.universe.enabled)
      - symbols in the static watchlist (config.data.watchlist)

    Orphaned OHLCV from symbols rotated out of previous universe runs is
    excluded — otherwise the table fills with stale rows whose news/model
    columns are blank by design, not by bug.

    Columns:
      symbol, daily_bars, latest_daily, hourly_bars, latest_hourly,
      news_total, news_scored, has_fundamentals, has_model
    """
    from pathlib import Path
    import pandas as pd
    from sqlalchemy import text
    from config.settings import config

    engine = get_engine()

    with engine.connect() as conn:
        # OHLCV counts + latest timestamp per symbol + interval
        bars_df = pd.read_sql(
            text("""
                SELECT symbol, interval,
                       COUNT(*) AS bar_count,
                       MAX(timestamp) AS latest_bar
                FROM ohlcv_bars
                WHERE symbol != '^VIX'
                GROUP BY symbol, interval
            """),
            conn,
        )

        # News totals + dominant source per symbol.
        # Source is inferred from article_id format:
        #   IBKR    → contains '$'  (e.g. "DJ-N$abc123")
        #   Alpaca  → all digits    (e.g. "38291847")
        #   yfinance → everything else (URLs / UUIDs)
        news_df = pd.read_sql(
            text("""
                SELECT symbol,
                       COUNT(*) AS news_total,
                       SUM(CASE WHEN sentiment_score IS NOT NULL THEN 1 ELSE 0 END)
                           AS news_scored,
                       SUM(CASE WHEN article_id LIKE '%$%' THEN 1 ELSE 0 END)
                           AS ibkr_count,
                       SUM(CASE WHEN article_id NOT LIKE '%$%'
                                 AND CAST(article_id AS TEXT) GLOB '[0-9]*'
                                THEN 1 ELSE 0 END)
                           AS alpaca_count
                FROM news_cache
                GROUP BY symbol
            """),
            conn,
        )

        # Symbols that have fundamentals
        fund_df = pd.read_sql(
            text("SELECT symbol FROM fundamental_data"),
            conn,
        )

        # Active universe assets (may include symbols not yet in ohlcv_bars)
        try:
            universe_df = pd.read_sql(
                text("SELECT symbol FROM universe_assets WHERE active = 1"),
                conn,
            )
        except Exception:
            universe_df = pd.DataFrame(columns=["symbol"])

    # Build the tracked-symbol list: active universe + static watchlist.
    # ohlcv_bars is NOT unioned in — orphan bars from rotated-out symbols
    # would otherwise appear as zero-news / no-model rows.
    universe_syms  = set(universe_df["symbol"].unique()) if not universe_df.empty else set()
    watchlist_syms = set(config.data.watchlist)
    all_symbol_set = universe_syms | watchlist_syms

    if not all_symbol_set:
        return pd.DataFrame()

    all_syms = pd.DataFrame({"symbol": sorted(all_symbol_set)})

    # Pivot intervals → daily / hourly columns (empty if no bar data at all)
    if not bars_df.empty:
        daily  = bars_df[bars_df["interval"] == "1d"].rename(
            columns={"bar_count": "daily_bars", "latest_bar": "latest_daily"}
        )[["symbol", "daily_bars", "latest_daily"]]

        hourly = bars_df[bars_df["interval"] == "1h"].rename(
            columns={"bar_count": "hourly_bars", "latest_bar": "latest_hourly"}
        )[["symbol", "hourly_bars", "latest_hourly"]]
    else:
        daily  = pd.DataFrame(columns=["symbol", "daily_bars", "latest_daily"])
        hourly = pd.DataFrame(columns=["symbol", "hourly_bars", "latest_hourly"])

    df = (
        all_syms
        .merge(daily,  on="symbol", how="left")
        .merge(hourly, on="symbol", how="left")
        .merge(news_df, on="symbol", how="left")
        .merge(fund_df.assign(has_fundamentals=True), on="symbol", how="left")
    )

    df["has_fundamentals"] = df["has_fundamentals"].fillna(False).astype(bool)
    df["news_total"]   = df["news_total"].fillna(0).astype(int)
    df["news_scored"]  = df["news_scored"].fillna(0).astype(int)
    df["ibkr_count"]   = df["ibkr_count"].fillna(0).astype(int)
    df["alpaca_count"] = df["alpaca_count"].fillna(0).astype(int)
    df["daily_bars"]   = df["daily_bars"].fillna(0).astype(int)
    df["hourly_bars"]  = df["hourly_bars"].fillna(0).astype(int)

    # Derive dominant news source per symbol
    def _news_source(row) -> str:
        if row["news_total"] == 0:
            return ""
        yf_count = row["news_total"] - row["ibkr_count"] - row["alpaca_count"]
        counts = {"IBKR": row["ibkr_count"], "Alpaca": row["alpaca_count"], "yfinance": yf_count}
        dominant = max(counts, key=counts.get)
        # Flag as Mixed if runner-up has meaningful share (>20% of total)
        others = sum(v for k, v in counts.items() if k != dominant)
        if others / row["news_total"] > 0.2:
            return f"{dominant}+"
        return dominant

    df["news_source"] = df.apply(_news_source, axis=1)

    # Convert latest timestamps to datetime
    for col in ("latest_daily", "latest_hourly"):
        df[col] = pd.to_datetime(df[col], errors="coerce")

    # Model checkpoints (filesystem check — not cached in DB)
    cache_root = Path("models/cache")
    def _has_model(sym: str) -> bool:
        base = cache_root / sym
        return (base / "lstm.pt").exists() and (base / "xgb.ubj").exists()

    df["has_model"] = df["symbol"].apply(_has_model)

    return df.sort_values("symbol").reset_index(drop=True)


# ── Risk & portfolio ──────────────────────────────────────────────────────────

@st.cache_data(ttl=30)   # short TTL — circuit breaker state can change quickly
def query_circuit_breaker_status() -> dict:
    """Return the current circuit-breaker status dict with timestamps in local time."""
    from risk.circuit_breaker import CircuitBreaker
    status = CircuitBreaker().get_status()
    for key in ("triggered_at", "reset_at", "recorded_at"):
        if status.get(key) is not None:
            status[key] = _to_local_dt(status[key])
    return status


@st.cache_data(ttl=30)
def query_circuit_breaker_log(limit: int = 50) -> pd.DataFrame:
    """Return recent circuit_breaker_log entries for display."""
    df = get_circuit_breaker_log(limit=limit)
    if df.empty:
        return df
    df = df.copy()
    for col in ("triggered_at", "reset_at", "recorded_at"):
        if col in df.columns:
            df[col] = _to_local_series(df[col])
    rename = {
        "event":           "Event",
        "reason":          "Reason",
        "daily_loss_pct":  "Daily Loss%",
        "weekly_loss_pct": "Weekly Loss%",
        "triggered_at":    "Triggered At",
        "reset_at":        "Reset At",
        "recorded_at":     "Recorded At",
    }
    return df.rename(columns=rename)


@st.cache_data(ttl=60)
def query_order_decisions(limit: int = 100, run_id: str = "") -> pd.DataFrame:
    """Return order decisions, newest first.  Pass run_id to filter to one run."""
    df = get_order_decisions(limit=limit)
    if df.empty:
        return df
    if run_id:
        df = df[df["run_id"] == run_id]

    if "decided_at" in df.columns:
        df = df.copy()
        df["decided_at"] = _to_local_series(df["decided_at"])

    rename = {
        "run_id":            "Run ID",
        "symbol":            "Symbol",
        "signal":            "Signal",
        "decision":          "Decision",
        "shares":            "Shares",
        "entry_price":       "Entry",
        "stop_price":        "Stop",
        "take_profit_price": "Take Profit",
        "position_value":    "Position $",
        "reject_reason":     "Reason",
        "decided_at":        "Decided At",
    }
    return df.rename(columns=rename)


@st.cache_data(ttl=60)
def query_distinct_run_ids(limit: int = 20) -> list[str]:
    """Return the most recent distinct signal-runner run IDs, newest first."""
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT run_id, MAX(decided_at) AS latest
            FROM order_decisions
            GROUP BY run_id
            ORDER BY latest DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
    return [r[0] for r in rows]


@st.cache_data(ttl=60)
def query_signal_runner_log(limit: int = 50) -> pd.DataFrame:
    """Return recent signal_runner_log entries for display."""
    df = get_signal_runner_log(limit=limit)
    if df.empty:
        return df
    df = df.copy()
    if "recorded_at" in df.columns:
        df["recorded_at"] = _to_local_series(df["recorded_at"])
    rename = {
        "run_id":                "Run ID",
        "run_date":              "Date",
        "mode":                  "Mode",
        "symbols_processed":     "Symbols",
        "signals_generated":     "Signals",
        "orders_submitted":      "Submitted",
        "orders_rejected":       "Rejected",
        "skipped_duplicates":    "Skipped",
        "skipped_stale":         "Stale",
        "longs_closed":          "Closed",
        "trailing_conversions":  "Trailing",
        "duration_seconds":      "Duration (s)",
        "recorded_at":           "Recorded At",
        "notes":                 "Notes",
    }
    return df.rename(columns=rename)


@st.cache_data(ttl=60)
def query_trailing_stop_log(
    limit: int = 100,
    run_id: str | None = None,
) -> pd.DataFrame:
    """Return recent trailing_stop_log entries for Page 8 display."""
    df = get_trailing_stop_log(limit=limit, run_id=run_id)
    if df.empty:
        return df
    df = df.copy()
    if "decided_at" in df.columns:
        df["decided_at"] = _to_local_series(df["decided_at"])
    rename = {
        "run_id":        "Run ID",
        "symbol":        "Symbol",
        "action":        "Action",
        "shares":        "Shares",
        "entry_price":   "Entry",
        "current_price": "Current",
        "atr":           "ATR",
        "trail_amount":  "Trail $",
        "reason":        "Reason",
        "decided_at":    "Decided At",
    }
    return df.rename(columns=rename)
