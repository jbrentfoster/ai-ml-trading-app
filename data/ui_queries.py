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
    get_intraday_run_log,
    get_latest_circuit_breaker_event,
    get_latest_indicators,
    get_order_decisions,
    get_recent_news,
    get_signal_runner_log,
    get_trade_log,
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
        "symbol":      r.symbol,
        "run_id":      r.run_id,
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
        # NULL on rows written before the 2026-05-12 migration; "dynamic" /
        # "static" thereafter.  Page 4 banners when any visible row is dynamic.
        "Universe Policy":    r.universe_policy,
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
        "stage3_score":      "Stage 3 Score",
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


@st.cache_data(ttl=300)
def query_held_symbols() -> list[str]:
    """
    Symbols currently held net-long per the live `fill_log` — the same audit
    trail Phase B reconciliation (and the one-time Flex backfill) populates.

    A symbol is "held" when SUM(BUY shares) - SUM(SELL shares) > 0 across all
    its recorded fills.  This is a DB-only proxy (no IBKR call), so it's safe
    to fan out from shared sidebar pickers used on every page.  It surfaces
    positions that have been rotated out of the active universe — exactly the
    case `signal_runner._fetch_held_long_symbols` unions into the daily run so
    orphan holds keep getting evaluated (e.g. VRT, dropped 2026-05-24 but still
    held).  Returns [] when `fill_log` is empty (pre-Phase-B databases).
    """
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT symbol
            FROM fill_log
            GROUP BY symbol
            HAVING SUM(CASE WHEN side = 'BUY' THEN shares ELSE -shares END) > 0
            """
        )).fetchall()
    return sorted({str(r[0]).upper() for r in rows if r[0]})


@st.cache_data(ttl=300)
def query_symbol_options() -> list[str]:
    """
    Return sorted symbols for sidebar pickers: the active Stage 3 universe
    UNION any currently-held positions (so orphan holds rotated out of the
    universe — e.g. VRT — remain selectable).  Falls back to
    `config.data.watchlist` when universe_assets is empty (e.g. the universe
    selector has never been run); held symbols are still unioned on top.
    """
    df = get_universe_assets(active_only=True)
    if df.empty or "symbol" not in df.columns:
        from config.settings import config
        base = {s.upper() for s in config.data.watchlist if s}
    else:
        base = set(df["symbol"].dropna().astype(str).str.upper().unique().tolist())
    base |= set(query_held_symbols())
    return sorted(base)


_NAME_SUFFIXES_TO_STRIP = (
    " Class A Common Stock",
    " Class B Common Stock",
    " Class C Common Stock",
    " Common Stock",
    " Ordinary Shares",
    " American Depositary Shares",
    " ADR",
)


def _clean_company_name(name: str) -> str:
    """Strip exchange-listing suffixes so titles read naturally."""
    n = (name or "").strip()
    for suffix in _NAME_SUFFIXES_TO_STRIP:
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    return n


@st.cache_data(ttl=86400, show_spinner=False)
def query_company_name(symbol: str) -> str:
    """
    Resolve a human-readable company name for `symbol`.

    Source order (cheapest first):
      1. `universe_assets.name` — already cached in SQLite for every universe candidate.
      2. yfinance `Ticker.info` (`longName`/`shortName`) — network call, cached for 24h
         so manual entries like AAPL only hit the network once per day.

    Returns "" when nothing is available; callers should treat empty as "no name known"
    and render the bare symbol.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return ""
    try:
        df = get_universe_assets(active_only=False)
        if not df.empty and "symbol" in df.columns and "name" in df.columns:
            hit = df.loc[df["symbol"].astype(str).str.upper() == sym, "name"]
            if not hit.empty:
                name = _clean_company_name(str(hit.iloc[0] or ""))
                if name:
                    return name
    except Exception:
        pass
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
        return _clean_company_name(info.get("longName") or info.get("shortName") or "")
    except Exception:
        return ""


def symbol_picker(
    label: str,
    default: str = "",
    key: str | None = None,
    sidebar: bool = True,
    help: str | None = None,
) -> str:
    """
    Editable combobox: options are the active Stage 3 universe symbols, but
    the user can also type a symbol that isn't on the list (manual override).
    `default` is selected initially; if it isn't in the options list it is
    prepended so the index lookup succeeds.
    """
    options = query_symbol_options()
    default_clean = (default or "").strip().upper()
    if default_clean and default_clean not in options:
        options = [default_clean] + options
    elif not default_clean:
        options = [""] + options
    container = st.sidebar if sidebar else st
    value = container.selectbox(
        label,
        options=options,
        index=options.index(default_clean) if default_clean in options else 0,
        accept_new_options=True,
        key=key,
        help=help or "Pick from the current Stage 3 universe or type any symbol.",
    )
    return (value or "").strip().upper()


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

        # Symbols that have fundamentals (table is append-only history; dedupe)
        fund_df = pd.read_sql(
            text("SELECT DISTINCT symbol FROM fundamental_data"),
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
@st.cache_data(ttl=60)
def query_intraday_run_log_today() -> pd.DataFrame:
    """Return today's intraday_run_log rows (newest first) for Page 8.

    Short TTL (60s) because intraday runs land mid-day and the operator may
    look at Page 8 immediately after an intraday slot fires.  Daily-runner
    tables use ttl=300 because their rows only land once per day.
    """
    today_local = datetime.now(timezone.utc).astimezone().date().strftime("%Y-%m-%d")
    df = get_intraday_run_log(limit=50, on_date=today_local)
    if df.empty:
        return df
    df = df.copy()
    if "run_timestamp" in df.columns:
        df["run_timestamp"] = _to_local_series(df["run_timestamp"])
    rename = {
        "run_id":              "Run ID",
        "run_timestamp":       "When",
        "mode":                "Mode",
        "status":              "Status",
        "daily_loss_pct":      "Daily Δ",
        "weekly_loss_pct":     "Weekly Δ",
        "cb_tripped":          "CB tripped",
        "positions_flattened": "Flattened",
        "trailing_evaluated":  "Trail eval",
        "trailing_ratcheted":  "Ratchets",
        "trailing_converted":  "Conversions",
        "duration_seconds":    "Duration (s)",
        "error_message":       "Error",
    }
    return df.rename(columns=rename)


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


# ── Trade history (closed trades from trade_log) ──────────────────────────────

def _active_universe_symbols() -> set[str]:
    """Symbols currently tracked by the system.

    Active universe assets when `universe_assets` is populated (the standard
    case once `universe_scheduler.py --run-now` has run); otherwise falls back
    to `config.data.watchlist` so static-watchlist mode still has a tracked
    set.  Returned upper-cased to match `trade_log.symbol` casing.
    """
    df = get_universe_assets(active_only=True)
    if not df.empty and "symbol" in df.columns:
        return {str(s).upper() for s in df["symbol"].dropna().tolist()}
    from config.settings import config
    return {str(s).upper() for s in config.data.watchlist if s}


def _filter_to_active_universe(df: pd.DataFrame) -> pd.DataFrame:
    """Drop walk_forward rows for symbols no longer in the active universe.

    Live rows (`source='live'`) always pass through — they represent actual
    broker fills, which must remain in the historical record regardless of
    whether the symbol is still being traded.  This is also a hard requirement
    for tax reporting: a real closed trade cannot disappear because the
    universe rotated.

    Without this filter, every weekly universe refresh leaves stale
    `walk_forward_results` rows from departed symbols, and the Page 10
    dedup-by-latest-run logic picks them up indefinitely — Summary cards then
    aggregate over a mix of current-universe and historical-only symbols,
    which inflates totals and mixes time periods.
    """
    if df.empty or "symbol" not in df.columns:
        return df
    tracked = _active_universe_symbols()
    if not tracked:
        return df  # No universe info to filter against — surface everything.
    is_live = df.get("source", pd.Series(index=df.index)).eq("live")
    in_universe = df["symbol"].astype(str).str.upper().isin(tracked)
    return df[is_live | in_universe].reset_index(drop=True)


def _keep_latest_run_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse walk_forward rows to the latest training run_id per symbol.

    The "latest run_id" is sourced from `walk_forward_results`, NOT from
    trade_log itself.  Reason: a fresh training run that produces zero
    closed trades (e.g. long-only gate suppressing every short, no buys
    firing in the test window) inserts no rows into trade_log.  Picking
    "latest run_id present in trade_log" then silently falls back to the
    *previous* run, surfacing stale pre-fix history that the current model
    no longer produces.  walk_forward_results writes one row per
    (run_id, symbol, fold_index) on every fold regardless of trade count,
    so it always reflects the current training session.

    Symbols whose latest WF run produced zero trades correctly disappear
    from the deduped view — that's the right semantic for "no trades in
    current model" (vs. "stale rows from a previous model").  Live rows
    pass through untouched.
    """
    if df.empty or "recorded_at" not in df.columns:
        return df
    wf_mask = df["source"] == "walk_forward"
    if not wf_mask.any():
        return df

    # Latest WF run_id per symbol from the per-fold results table.
    engine = get_engine()
    with Session(engine) as session:
        wf_runs = session.query(
            WalkForwardResult.symbol,
            WalkForwardResult.run_id,
            WalkForwardResult.recorded_at,
        ).all()
    if not wf_runs:
        return df  # No WF training has run yet — nothing authoritative to dedup against.

    runs_df = pd.DataFrame(wf_runs, columns=["symbol", "run_id", "recorded_at"])
    latest = (
        runs_df.groupby(["symbol", "run_id"])["recorded_at"].max()
              .reset_index()
              .sort_values("recorded_at", ascending=False)
              .drop_duplicates("symbol")[["symbol", "run_id"]]
    )

    wf = df[wf_mask]
    other = df[~wf_mask]
    wf_kept = wf.merge(latest, on=["symbol", "run_id"], how="inner")
    return pd.concat([wf_kept, other], ignore_index=True)


@st.cache_data(ttl=300)
def query_trade_log(
    source: str | None = None,
    symbols: tuple[str, ...] | None = None,
    start_date=None,
    end_date=None,
    exit_reasons: tuple[str, ...] | None = None,
    run_id: str = "",
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> pd.DataFrame:
    """
    Return closed-trade rows from trade_log with derived columns.

    Adds:
      - holding_days  = (exit_ts − entry_ts).days
      - is_long_term  = holding_days > 365
      - net_pnl       = pnl                    # ``pnl`` is *already* net of costs
      - gross_pnl     = pnl + costs_charged    # back-derived for display

    The ``trade_log.pnl`` column is written by ``walk_forward.py:_close_trade``
    as ``pnl_pct × entry_px × shares`` — and ``pnl_pct = gross_pct − total_costs``,
    so the stored ``pnl`` is the realised *net* dollar P&L.  Subtracting
    ``costs_charged`` again here would double-count fees.  Phase A picked this
    convention so that ``pnl_pct`` remains a valid input for realised-Kelly
    (Phase C); this query layer just exposes both views to the dashboard.
    Date range is applied against exit_ts (the realisation date — what matters
    for tax-year bucketing).  Symbols and exit_reasons are tuples so the
    @st.cache_data hash is stable.

    When `dedup_to_latest_run=True` (default) and no specific `run_id` is
    requested, walk_forward rows are collapsed to the latest training run per
    symbol — without this, every weekly `--force` retrain stacks an extra copy
    of every closed trade onto the page and inflates summary metrics.  Pass
    `False` to see the full multi-run history (or filter by a specific run_id,
    which short-circuits the dedup).

    When `active_universe_only=True` (default), walk_forward rows for symbols
    no longer in the active universe are dropped.  Live rows always pass
    through.  Pass `False` to see the full historical record across all
    symbols ever trained (useful for auditing universe-rotation effects).
    """
    df = get_trade_log(source=source if source else None)
    if df.empty:
        return df

    df = df.copy()

    if active_universe_only:
        df = _filter_to_active_universe(df)

    if df.empty:
        return df

    if symbols:
        df = df[df["symbol"].isin(symbols)]
    if start_date is not None:
        df = df[df["exit_ts"] >= pd.Timestamp(start_date)]
    if end_date is not None:
        df = df[df["exit_ts"] < pd.Timestamp(end_date) + pd.Timedelta(days=1)]
    if exit_reasons:
        df = df[df["exit_reason"].isin(exit_reasons)]
    if run_id:
        df = df[df["run_id"] == run_id]
    elif dedup_to_latest_run:
        df = _keep_latest_run_per_symbol(df)

    if df.empty:
        return df

    # Derived columns (computed on entry_ts/exit_ts before tz conversion so the
    # holding_days math is in UTC — same instant in every timezone).
    df["holding_days"] = (df["exit_ts"] - df["entry_ts"]).dt.days
    df["is_long_term"] = df["holding_days"] > 365
    # ``pnl`` is already net of costs (see docstring); back-derive gross for display.
    costs              = df["costs_charged"].fillna(0.0)
    df["net_pnl"]      = df["pnl"]
    df["gross_pnl"]    = df["pnl"] + costs

    # Display columns shifted to local for readability.
    df["entry_ts"]    = _to_local_series(df["entry_ts"])
    df["exit_ts"]     = _to_local_series(df["exit_ts"])
    df["recorded_at"] = _to_local_series(df["recorded_at"])

    return df.reset_index(drop=True)


@st.cache_data(ttl=300)
def query_trade_summary(
    source: str | None = None,
    symbols: tuple[str, ...] | None = None,
    start_date=None,
    end_date=None,
    exit_reasons: tuple[str, ...] | None = None,
    run_id: str = "",
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> dict:
    """
    Aggregates for the summary cards on the Trade History page.

    Returns a dict with: n_trades, gross_pnl, total_costs, net_pnl, win_rate.
    win_rate is computed against net_pnl (i.e. fees count against you).
    """
    df = query_trade_log(
        source=source, symbols=symbols,
        start_date=start_date, end_date=end_date,
        exit_reasons=exit_reasons, run_id=run_id,
        dedup_to_latest_run=dedup_to_latest_run,
        active_universe_only=active_universe_only,
    )
    if df.empty:
        return {
            "n_trades":    0,
            "gross_pnl":   0.0,
            "total_costs": 0.0,
            "net_pnl":     0.0,
            "win_rate":    0.0,
        }
    n_trades  = len(df)
    # gross_pnl is back-derived in query_trade_log — using df["pnl"] here would
    # display the *net* number under a "Gross" label (the original Page 10 bug).
    gross_pnl = float(df["gross_pnl"].sum())
    total_costs = float(df["costs_charged"].fillna(0.0).sum())
    net_pnl   = float(df["net_pnl"].sum())
    n_wins    = int((df["net_pnl"] > 0).sum())
    win_rate  = n_wins / n_trades if n_trades else 0.0
    return {
        "n_trades":    n_trades,
        "gross_pnl":   gross_pnl,
        "total_costs": total_costs,
        "net_pnl":     net_pnl,
        "win_rate":    win_rate,
    }


@st.cache_data(ttl=300)
def query_tax_breakdown(
    source: str | None = None,
    symbols: tuple[str, ...] | None = None,
    start_date=None,
    end_date=None,
    exit_reasons: tuple[str, ...] | None = None,
    run_id: str = "",
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> dict:
    """
    Short-term vs long-term realised gain/loss aggregates.

    Tax rates are intentionally NOT applied here — they live in the page's
    session state so the user can fiddle without invalidating cache.

    Returns a dict with eight figures (all dollar amounts, all positive
    magnitudes for gains/losses), plus the net of each class:
      st_gain, st_loss, st_net,
      lt_gain, lt_loss, lt_net,
      total_net, n_st, n_lt
    """
    df = query_trade_log(
        source=source, symbols=symbols,
        start_date=start_date, end_date=end_date,
        exit_reasons=exit_reasons, run_id=run_id,
        dedup_to_latest_run=dedup_to_latest_run,
        active_universe_only=active_universe_only,
    )
    if df.empty:
        return {
            "st_gain": 0.0, "st_loss": 0.0, "st_net": 0.0,
            "lt_gain": 0.0, "lt_loss": 0.0, "lt_net": 0.0,
            "total_net": 0.0, "n_st": 0, "n_lt": 0,
        }
    st_df = df[~df["is_long_term"]]
    lt_df = df[ df["is_long_term"]]

    st_gain = float(st_df.loc[st_df["net_pnl"] > 0, "net_pnl"].sum())
    st_loss = float(-st_df.loc[st_df["net_pnl"] < 0, "net_pnl"].sum())
    lt_gain = float(lt_df.loc[lt_df["net_pnl"] > 0, "net_pnl"].sum())
    lt_loss = float(-lt_df.loc[lt_df["net_pnl"] < 0, "net_pnl"].sum())

    return {
        "st_gain": st_gain, "st_loss": st_loss, "st_net": st_gain - st_loss,
        "lt_gain": lt_gain, "lt_loss": lt_loss, "lt_net": lt_gain - lt_loss,
        "total_net": (st_gain - st_loss) + (lt_gain - lt_loss),
        "n_st": int(len(st_df)),
        "n_lt": int(len(lt_df)),
    }


@st.cache_data(ttl=300)
def query_trade_log_filter_options(
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> dict:
    """
    Distinct values present in trade_log for populating page filters.

    Returns: {"symbols": [...], "exit_reasons": [...], "sources": [...]}
    Each list is sorted; empty lists when the table has no rows yet.

    Both flags should match the page's view kwargs so the dropdown matches
    what ``query_trade_log`` will actually return.

    When ``dedup_to_latest_run=True`` (default), walk_forward rows are first
    collapsed to the latest training run per symbol via
    ``_keep_latest_run_per_symbol``.  Without this, symbols whose latest WF
    run produced zero closed trades stayed selectable in the dropdown but
    yielded an empty table (observed 2026-05-04 during long-only gate
    verification).

    When ``active_universe_only=True`` (default), walk_forward rows for
    symbols not in the active universe are dropped before extracting the
    distinct lists.  Live rows always pass through.
    """
    df = get_trade_log()
    if df.empty:
        return {"symbols": [], "exit_reasons": [], "sources": []}
    if active_universe_only:
        df = _filter_to_active_universe(df)
        if df.empty:
            return {"symbols": [], "exit_reasons": [], "sources": []}
    if dedup_to_latest_run:
        df = _keep_latest_run_per_symbol(df)
        if df.empty:
            return {"symbols": [], "exit_reasons": [], "sources": []}
    return {
        "symbols":      sorted(df["symbol"].dropna().unique().tolist()),
        "exit_reasons": sorted(df["exit_reason"].dropna().unique().tolist()),
        "sources":      sorted(df["source"].dropna().unique().tolist()),
    }


@st.cache_data(ttl=300)
def query_benchmark_returns(
    source: str | None = None,
    symbols: tuple[str, ...] | None = None,
    start_date=None,
    end_date=None,
    exit_reasons: tuple[str, ...] | None = None,
    run_id: str = "",
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> pd.DataFrame:
    """Return trades eligible for benchmark-relative analysis.

    Thin wrapper over ``query_trade_log`` that:
      - Drops rows where ``benchmark_return_pct IS NULL`` (no benchmark bar on
        entry or exit — would silently distort aggregates).
      - Adds an ``excess_pct`` column = ``pnl_pct − benchmark_return_pct``.

    ``fold_end`` rows are intentionally NOT filtered here — they are real
    rows with valid benchmark returns; the *interpretation* (backtest artifact
    vs strategy decision) is a Page 10 concern.  Callers exclude them from
    headline metrics; the per-trade audit toggle allows including them.

    See the "Fold-end closures are backtest artifacts, not strategy decisions"
    architectural-decision note in CLAUDE.md for the rationale.
    """
    df = query_trade_log(
        source=source,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        exit_reasons=exit_reasons,
        run_id=run_id,
        dedup_to_latest_run=dedup_to_latest_run,
        active_universe_only=active_universe_only,
    )
    if df.empty or "benchmark_return_pct" not in df.columns:
        return df
    df = df[df["benchmark_return_pct"].notna()].copy()
    if df.empty:
        return df
    df["excess_pct"] = df["pnl_pct"] - df["benchmark_return_pct"]
    return df.reset_index(drop=True)


@st.cache_data(ttl=300)
def query_capital_weighted_roi(
    symbols: tuple[str, ...] | None = None,
    start_date=None,
    end_date=None,
    exit_reasons: tuple[str, ...] | None = None,
    run_id: str = "",
    dedup_to_latest_run: bool = True,
    active_universe_only: bool = True,
) -> dict:
    """Capital-weighted ROI of live fills vs a hypothetical benchmark position
    that deployed the SAME dollars over each trade's holding period.

    **Scoped to ``source='live'`` only** — this answers "did the money I
    actually put at risk beat just holding the benchmark?", which is only
    meaningful for real broker fills.  ``walk_forward`` rows are Kelly-sized
    synthetic trades against an *assumed* equity base, not real capital, so
    summing their notional would produce a meaningless ROI denominator.  The
    source is forced here regardless of the page's Source radio.

    Per trade::

        capital_i       = shares × entry_px                  (dollars deployed)
        strategy_pnl_i  = net_pnl (== trade_log.pnl)         (NET of commissions)
        benchmark_pnl_i = capital_i × benchmark_return_pct   (RAW benchmark return)

    Capital-weighted aggregates::

        strategy_roi  = Σ strategy_pnl  / Σ capital
        benchmark_roi = Σ benchmark_pnl / Σ capital
        dollar_diff   = Σ strategy_pnl − Σ benchmark_pnl

    The strategy side is NET of fees; the benchmark side is RAW — a buy-and-hold
    benchmark position pays no trading commissions, so "my P&L net of friction
    vs a frictionless benchmark" is exactly the retail-alpha frame.  This is the
    same deliberate asymmetry the per-trade ``excess_pct`` metric uses — see the
    CLAUDE.md note "Benchmark-relative tracking uses raw SPY return vs net trade
    P&L".  Do NOT subtract ``costs_charged`` from the strategy side: ``pnl`` is
    already net (double-count footgun, same as ``query_trade_log``).

    Rows with NULL ``benchmark_return_pct`` are excluded (no benchmark bar on
    entry or exit) so both sides share an identical capital base.

    Returns a dict::

        n_trades, capital_deployed, strategy_pnl, benchmark_pnl,
        strategy_roi, benchmark_roi, roi_diff_pct, dollar_diff

    All-zero dict when no eligible live rows exist.  ``strategy_roi`` /
    ``benchmark_roi`` / ``roi_diff_pct`` are fractions (0.05 == 5%).
    """
    empty = {
        "n_trades": 0, "capital_deployed": 0.0,
        "strategy_pnl": 0.0, "benchmark_pnl": 0.0,
        "strategy_roi": 0.0, "benchmark_roi": 0.0,
        "roi_diff_pct": 0.0, "dollar_diff": 0.0,
    }

    df = query_trade_log(
        source="live",
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        exit_reasons=exit_reasons,
        run_id=run_id,
        dedup_to_latest_run=dedup_to_latest_run,
        active_universe_only=active_universe_only,
    )
    if df.empty or "benchmark_return_pct" not in df.columns:
        return empty
    df = df[df["benchmark_return_pct"].notna()].copy()
    if df.empty:
        return empty

    capital = df["shares"].fillna(0.0) * df["entry_px"].fillna(0.0)
    total_capital = float(capital.sum())
    strategy_pnl  = float(df["net_pnl"].sum())
    benchmark_pnl = float((capital * df["benchmark_return_pct"]).sum())

    if total_capital <= 0:
        # Degenerate capital base (no positive notional) — can't form an ROI
        # denominator.  Report the dollar figures so the section isn't blank,
        # but leave the ratios at 0 rather than dividing by ~0.
        return {
            **empty,
            "n_trades":         int(len(df)),
            "capital_deployed": total_capital,
            "strategy_pnl":     strategy_pnl,
            "benchmark_pnl":    benchmark_pnl,
            "dollar_diff":      strategy_pnl - benchmark_pnl,
        }

    strategy_roi  = strategy_pnl / total_capital
    benchmark_roi = benchmark_pnl / total_capital
    return {
        "n_trades":         int(len(df)),
        "capital_deployed": total_capital,
        "strategy_pnl":     strategy_pnl,
        "benchmark_pnl":    benchmark_pnl,
        "strategy_roi":     strategy_roi,
        "benchmark_roi":    benchmark_roi,
        "roi_diff_pct":     strategy_roi - benchmark_roi,
        "dollar_diff":      strategy_pnl - benchmark_pnl,
    }


@st.cache_data(ttl=60)
def query_distinct_trade_log_run_ids(limit: int = 20) -> list[dict]:
    """
    Return the most recent distinct run_ids in trade_log, newest first.

    Each dict has: run_id, source, latest (datetime in local tz), n_trades.
    Different from query_distinct_run_ids (Page 8) — those are signal_runner
    run_ids from order_decisions; these come from trade_log and today are
    walk_forward training run_ids.  Once Phase B lands, source='live' rows
    will share run_ids with the signal_runner table.
    """
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT run_id, source,
                   MAX(recorded_at) AS latest,
                   COUNT(*)         AS n_trades
            FROM trade_log
            WHERE run_id IS NOT NULL
            GROUP BY run_id, source
            ORDER BY latest DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
    out = []
    for r in rows:
        latest = pd.to_datetime(r[2]) if r[2] is not None else None
        out.append({
            "run_id":   r[0],
            "source":   r[1],
            "latest":   _to_local_dt(latest.to_pydatetime()) if latest is not None else None,
            "n_trades": int(r[3]),
        })
    return out


# ── LLM news analysis (shadow workflow — Page 11) ─────────────────────────────

@st.cache_data(ttl=120)
def query_llm_news_analysis(
    symbols: tuple | None = None,
    days: int = 14,
    model: str | None = None,
    attributed: bool = False,
) -> pd.DataFrame:
    """LLM news-analysis rows for the dashboard (newest first), event times in
    local wall-clock.  ``attributed=True`` filters/keys on the ticker the
    article is *about* rather than the IBKR feed tag."""
    from data.database import get_llm_analysis
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    syms = list(symbols) if symbols else None
    df = get_llm_analysis(symbols=syms, since=since, model=model, attributed=attributed)
    if df.empty:
        return df
    df["published_at"] = _to_local_series(df["published_at"])
    if "scored_at" in df.columns:
        df["scored_at"] = _to_local_series(df["scored_at"])

    # Resolve attribution at READ time from the stored primary_entity, so the
    # heuristic can improve without re-running the 8B over every article.
    from data.database import build_company_name_map
    from models.llm_analyst import (
        resolve_attribution_status, status_is_mismatch, ATTR_DIGEST,
    )
    name_map = build_company_name_map()
    resolved = df.apply(
        lambda r: resolve_attribution_status(
            r["primary_entity"], r["symbol"], name_map, headline=r.get("headline")),
        axis=1)
    df["attributed_symbol"] = [a for a, _ in resolved]
    df["attribution_status"] = [s for _, s in resolved]
    # Boolean kept for back-compat; digests are NOT mismatches (the feed symbol
    # legitimately appears among the many companies a roundup covers).
    df["attribution_mismatch"] = df["attribution_status"].map(status_is_mismatch)
    df["is_digest"] = df["attribution_status"] == ATTR_DIGEST

    # Cluster near-duplicate articles into events (read-time — see
    # data/news_dedup.py).  Adds event_id / event_size / is_representative, plus
    # the per-event composite-score spread so model inconsistency stays visible.
    from data.news_dedup import cluster_news_events
    clustered = cluster_news_events(df.to_dict("records"))
    df = pd.DataFrame(clustered)
    if not df.empty:
        df = df.sort_values("published_at", ascending=False).reset_index(drop=True)
        grp = df.groupby("event_id")["composite_score"]
        df["event_score_min"] = df["event_id"].map(grp.min())
        df["event_score_max"] = df["event_id"].map(grp.max())
    return df


@st.cache_data(ttl=600)
def query_news_body(symbol: str, article_id: str) -> str | None:
    """Full article text for one article (lazy — bodies are large, so they're
    fetched on demand from the drill-down rather than loaded into the frame)."""
    from data.database import get_news_body
    return get_news_body(symbol, article_id)


@st.cache_data(ttl=120)
def query_llm_analysis_options(days: int = 30) -> dict:
    """Distinct models + symbols present in llm_news_analysis (for filters)."""
    from data.database import get_llm_analysis
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    df = get_llm_analysis(since=since)
    if df.empty:
        return {"models": [], "symbols": []}
    return {
        "models":  sorted(df["model"].dropna().unique().tolist()),
        "symbols": sorted(df["symbol"].dropna().unique().tolist()),
    }
