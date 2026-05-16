"""
Settings — Page 5
==================
View and edit all application settings.  Changes are saved to
config/settings.yaml and take effect immediately in the running app.

API keys (ALPACA_API_KEY, ALPACA_SECRET_KEY) are managed via environment
variables / .env and are never stored in settings.yaml.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from config.settings import config, save_yaml_config, TradingMode

st.set_page_config(
    page_title="Settings",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("⚙️ Settings")
st.caption(
    "Changes are written to `config/settings.yaml` and applied immediately.  "
    "Restart the app to pick up changes in background worker processes."
)

# ── Helper to show a save banner ──────────────────────────────────────────────

def _save_and_notify():
    save_yaml_config()
    st.success("Settings saved to `config/settings.yaml`.")


# ── Tabs ──────────────────────────────────────────────────────────────────────

tabs = st.tabs([
    "Watchlist & Data",
    "Universe",
    "Trading",
    "ML / Models",
    "News & Sentiment",
    "IBKR Connection",
    "Logging",
])

# ═════════════════════════════════════════════════════════════════════════════
# TAB 1 — WATCHLIST & DATA
# ═════════════════════════════════════════════════════════════════════════════
with tabs[0]:
    st.subheader("Watchlist & Data Pipeline")

    with st.form("form_data"):
        watchlist_raw = st.text_area(
            "Watchlist (one ticker per line)",
            value="\n".join(config.data.watchlist),
            height=220,
            help="Symbols that will be fetched, shown in the dashboard, and used for ML training.",
        )

        c1, c2 = st.columns(2)
        daily_lookback = c1.number_input(
            "Daily bar history (days)", min_value=30, max_value=1825,
            value=config.data.daily_lookback_days,
            help="How many days of daily OHLCV history to fetch on first run.",
        )
        intraday_lookback = c2.number_input(
            "Intraday history (days)", min_value=1, max_value=59,
            value=config.data.intraday_lookback_days,
            help="yfinance caps 1 h data at ~59 days.",
        )

        intraday_interval = st.selectbox(
            "Intraday interval", ["1h", "30m", "15m", "5m"],
            index=["1h", "30m", "15m", "5m"].index(config.data.intraday_interval),
        )

        db_path = st.text_input("SQLite database path", value=config.data.db_path)

        if st.form_submit_button("Save", type="primary"):
            tickers = [t.strip().upper() for t in watchlist_raw.splitlines() if t.strip()]
            if not tickers:
                st.error("Watchlist cannot be empty.")
            else:
                config.data.watchlist               = tickers
                config.data.daily_lookback_days     = int(daily_lookback)
                config.data.intraday_lookback_days  = int(intraday_lookback)
                config.data.intraday_interval       = intraday_interval
                config.data.db_path                 = db_path
                _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 2 — UNIVERSE
# ═════════════════════════════════════════════════════════════════════════════
with tabs[1]:
    st.subheader("Automated Universe Selection")
    st.markdown(
        "When **enabled**, the data pipeline replaces the static watchlist with a "
        "dynamic three-stage funnel (Alpaca → liquidity filter → XGBoost ranking). "
        "Permanent fixtures (index & sector ETFs) are always included regardless."
    )

    with st.form("form_universe"):
        enabled = st.toggle(
            "Enable automated universe",
            value=config.universe.enabled,
            help="When on, run_pipeline.py uses the funnel instead of the Watchlist. "
                 "Use --use-watchlist to override on a single run.",
        )
        if enabled:
            st.info(
                "Universe is **enabled**. Run `python universe_scheduler.py --run-now` "
                "or click **Full Refresh** on the Universe page to populate the candidate list."
            )
        else:
            st.caption("Universe is disabled — the static watchlist above is used by the pipeline.")

        st.divider()
        st.markdown("**Funnel thresholds**")

        uc1, uc2, uc3 = st.columns(3)
        stage1_max = uc1.number_input(
            "Stage 1 max assets", 100, 100000, config.universe.stage1_max, step=500,
            help="Maximum equities to pull from Alpaca (active, tradable US stocks).",
        )
        stage2_max = uc2.number_input(
            "Stage 2 max assets", 10, 2000, config.universe.stage2_max, step=50,
            help="Cap after liquidity / market-cap filter.",
        )
        stage3_max = uc3.number_input(
            "Stage 3 max candidates", 5, 500, config.universe.stage3_max, step=5,
            help="Final ranked candidates passed to the pipeline.",
        )

        fc1, fc2 = st.columns(2)
        min_mkt_cap = fc1.number_input(
            "Min market cap ($B)", 0.1, 100.0,
            config.universe.min_market_cap / 1e9, 0.5, format="%.1f",
            help="Symbols below this market cap are dropped in Stage 2.",
        )
        min_dv = fc2.number_input(
            "Min avg daily $ volume ($M)", 0.5, 100.0,
            config.universe.min_avg_dollar_volume / 1e6, 0.5, format="%.1f",
            help="Average (close × volume) over 20 bars must exceed this.",
        )

        st.divider()
        st.markdown("**Exchange filter** (fixtures bypass this check)")
        st.caption(
            "Foreign ADRs typically trade on OTC. Leaving OTC out keeps the universe "
            "to domestic US-listed equities. Clear the list to allow all exchanges."
        )
        exchanges_raw = st.text_input(
            "Allowed exchanges (comma-separated)",
            value=", ".join(config.universe.allowed_exchanges),
            help="e.g. NYSE, NASDAQ, ARCA, BATS — OTC excluded = no foreign ADRs",
        )

        st.divider()
        st.markdown("**Permanent fixtures** (one ticker per line — always included)")
        fixtures_raw = st.text_area(
            "Fixtures",
            value="\n".join(config.universe.permanent_fixtures),
            height=160,
            help="These symbols bypass all funnel filters and are always in the active list.",
        )

        st.divider()
        run_log_retention = st.number_input(
            "Run log retention (days)", 7, 365, config.universe.run_log_retention_days,
            help="Universe run log entries older than this are eligible for pruning.",
        )

        if st.form_submit_button("Save", type="primary"):
            fixtures  = [t.strip().upper() for t in fixtures_raw.splitlines() if t.strip()]
            exchanges = [e.strip().upper() for e in exchanges_raw.split(",") if e.strip()]
            config.universe.enabled                = bool(enabled)
            config.universe.stage1_max             = int(stage1_max)
            config.universe.stage2_max             = int(stage2_max)
            config.universe.stage3_max             = int(stage3_max)
            config.universe.min_market_cap         = float(min_mkt_cap) * 1e9
            config.universe.min_avg_dollar_volume  = float(min_dv) * 1e6
            config.universe.allowed_exchanges      = exchanges
            config.universe.permanent_fixtures     = fixtures
            config.universe.run_log_retention_days = int(run_log_retention)
            _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 3 — TRADING
# ═════════════════════════════════════════════════════════════════════════════
with tabs[2]:
    st.subheader("Trading Parameters")

    with st.form("form_trading"):
        mode_options = ["simulation", "live"]
        mode_idx     = 0 if config.trading.mode == TradingMode.SIMULATION else 1
        mode_sel     = st.selectbox(
            "Trading mode", mode_options, index=mode_idx,
            help="SIMULATION uses the IBKR paper account.  Switch to LIVE only after thorough paper testing.",
        )
        if mode_sel == "live":
            st.warning("**LIVE mode** will place real orders against your funded account.")

        c1, c2 = st.columns(2)
        max_pos = c1.number_input(
            "Max position size (% of equity)", min_value=0.01, max_value=0.50,
            value=config.trading.max_position_size_pct,
            format="%.3f",
            help="Maximum fraction of total equity for any single trade.",
        )
        max_dd = c2.number_input(
            "Max portfolio drawdown (%)", min_value=0.01, max_value=0.50,
            value=config.trading.max_portfolio_drawdown_pct,
            format="%.3f",
            help="Stop all new trades when portfolio drawdown exceeds this level.",
        )

        c3, c4 = st.columns(2)
        stop_loss = c3.number_input(
            "Default stop-loss (%)", min_value=0.001, max_value=0.20,
            value=config.trading.default_stop_loss_pct,
            format="%.3f",
        )
        take_profit = c4.number_input(
            "Default take-profit (%)", min_value=0.001, max_value=0.50,
            value=config.trading.default_take_profit_pct,
            format="%.3f",
        )

        cash_reserve = st.number_input(
            "Cash reserve (% of equity to keep uninvested)",
            min_value=0.0, max_value=0.90,
            value=config.trading.cash_reserve_pct,
            format="%.2f",
            help=(
                "Fraction of total equity held as cash at all times. "
                "Position sizes are calculated against the remaining investable capital. "
                "e.g. 0.20 = keep 20% cash; a 5% Kelly position becomes 5% of 80% = 4% of total equity."
            ),
        )

        if st.form_submit_button("Save", type="primary"):
            config.trading.mode                       = TradingMode(mode_sel)
            config.trading.max_position_size_pct      = float(max_pos)
            config.trading.max_portfolio_drawdown_pct = float(max_dd)
            config.trading.default_stop_loss_pct      = float(stop_loss)
            config.trading.default_take_profit_pct    = float(take_profit)
            config.trading.cash_reserve_pct           = float(cash_reserve)
            _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 4 — ML / MODELS
# ═════════════════════════════════════════════════════════════════════════════
with tabs[3]:
    st.subheader("ML / Model Settings")

    with st.form("form_ml"):

        # ── Ensemble weights ──────────────────────────────────────────────────
        st.markdown("**Ensemble Weights** (initial values before walk-forward rebalancing)")
        ec1, ec2, ec3 = st.columns(3)
        w_lstm    = ec1.number_input("LSTM",    0.10, 0.80, config.ml.ensemble_lstm_weight,    0.05, format="%.2f")
        w_xgb     = ec2.number_input("XGBoost", 0.10, 0.80, config.ml.ensemble_xgb_weight,     0.05, format="%.2f")
        w_finbert = ec3.number_input("FinBERT", 0.10, 0.80, config.ml.ensemble_finbert_weight, 0.05, format="%.2f")
        wt = w_lstm + w_xgb + w_finbert
        if abs(wt - 1.0) > 0.001:
            st.warning(f"Weights sum to {wt:.3f} — they will be normalised to 1.0 on save.")

        sc1, sc2 = st.columns(2)
        weight_floor = sc1.number_input("Weight floor",  0.05, 0.33, config.ml.ensemble_weight_floor, 0.05, format="%.2f")
        nudge        = sc2.number_input("Rebalance nudge", 0.01, 0.20, config.ml.ensemble_nudge,       0.01, format="%.2f")

        st.divider()

        # ── Signal gate ───────────────────────────────────────────────────────
        st.markdown("**Signal Gate**")
        gc1, gc2, gc3 = st.columns(3)
        threshold    = gc1.number_input("Signal threshold", 0.10, 0.90, config.ml.signal_threshold,   0.05, format="%.2f")
        confirmation = gc2.number_input("Models must agree", 1, 3, config.ml.signal_confirmation, step=1,
                                        help="Number of the 3 models that must agree on direction.")
        lookback_days = gc3.number_input(
            "Signal page lookback (days)", min_value=30, max_value=1825,
            value=config.ml.signal_lookback_days, step=30,
            help="Default date range shown on the Model Signals page.",
        )

        st.divider()

        # ── Walk-forward ──────────────────────────────────────────────────────
        st.markdown("**Walk-Forward Training**")
        wc1, wc2, wc3, wc4 = st.columns(4)
        wf_train = wc1.number_input("Train bars",  30, 500, config.ml.wf_train_bars, step=10)
        wf_test  = wc2.number_input("Test bars",    5, 126, config.ml.wf_test_bars,  step=5)
        wf_gap   = wc3.number_input("Gap bars",     0,  10, config.ml.wf_gap_bars,   step=1)
        wf_splits= wc4.number_input("# Splits",     2,  20, config.ml.wf_n_splits,   step=1)
        min_bars = int(wf_train) + int(wf_gap) + int(wf_splits) * int(wf_test)
        st.caption(f"Minimum dataset bars required: **{min_bars}**")

        st.divider()

        # ── Cost model ────────────────────────────────────────────────────────
        st.markdown("**Cost Model** (applied on each signal bar)")
        cc1, cc2 = st.columns(2)
        slippage   = cc1.number_input("Slippage per side (%)", 0.0, 0.01,
                                      config.ml.slippage_pct, 0.0001, format="%.4f")
        commission = cc2.number_input("Commission per share ($)", 0.0, 0.05,
                                      config.ml.commission_per_share, 0.001, format="%.4f")

        st.divider()

        # ── LSTM hyperparams ──────────────────────────────────────────────────
        with st.expander("LSTM Hyperparameters", expanded=False):
            lc1, lc2, lc3 = st.columns(3)
            lstm_seq   = lc1.number_input("Sequence length", 10, 250, config.ml.lstm_sequence_length, step=10)
            lstm_hid   = lc2.number_input("Hidden size",     32, 512, config.ml.lstm_hidden_size,     step=32)
            lstm_layers= lc3.number_input("Layers",           1,   6, config.ml.lstm_num_layers,      step=1)
            lc4, lc5, lc6 = st.columns(3)
            lstm_drop  = lc4.number_input("Dropout", 0.0, 0.5, config.ml.lstm_dropout, 0.05, format="%.2f")
            lstm_ep    = lc5.number_input("Epochs",    1, 500, config.ml.lstm_epochs,  step=10)
            lstm_lr    = lc6.number_input("Learning rate", 1e-5, 1e-2,
                                          config.ml.lstm_learning_rate, 1e-4, format="%.5f")
            lstm_bs    = st.number_input("Batch size", 8, 256, config.ml.lstm_batch_size, step=8)

        # ── XGBoost hyperparams ───────────────────────────────────────────────
        with st.expander("XGBoost Hyperparameters", expanded=False):
            xc1, xc2, xc3 = st.columns(3)
            xgb_n   = xc1.number_input("n_estimators",  50, 2000, config.ml.xgb_n_estimators, step=50)
            xgb_d   = xc2.number_input("max_depth",       1,   15, config.ml.xgb_max_depth,    step=1)
            xgb_lr  = xc3.number_input("learning_rate", 0.001, 0.5,
                                        config.ml.xgb_learning_rate, 0.01, format="%.3f")
            xc4, xc5 = st.columns(2)
            xgb_sub = xc4.number_input("subsample",     0.3, 1.0, config.ml.xgb_subsample,  0.05, format="%.2f")
            xgb_col = xc5.number_input("colsample",     0.3, 1.0, config.ml.xgb_colsample,  0.05, format="%.2f")

        if st.form_submit_button("Save", type="primary"):
            # Normalise weights
            total = w_lstm + w_xgb + w_finbert
            config.ml.ensemble_lstm_weight    = round(w_lstm    / total, 6)
            config.ml.ensemble_xgb_weight     = round(w_xgb     / total, 6)
            config.ml.ensemble_finbert_weight = round(w_finbert / total, 6)
            config.ml.ensemble_weight_floor   = float(weight_floor)
            config.ml.ensemble_nudge          = float(nudge)

            config.ml.signal_threshold    = float(threshold)
            config.ml.signal_confirmation = int(confirmation)
            config.ml.signal_lookback_days = int(lookback_days)

            config.ml.wf_train_bars = int(wf_train)
            config.ml.wf_test_bars  = int(wf_test)
            config.ml.wf_gap_bars   = int(wf_gap)
            config.ml.wf_n_splits   = int(wf_splits)

            config.ml.slippage_pct          = float(slippage)
            config.ml.commission_per_share  = float(commission)

            config.ml.lstm_sequence_length = int(lstm_seq)
            config.ml.lstm_hidden_size     = int(lstm_hid)
            config.ml.lstm_num_layers      = int(lstm_layers)
            config.ml.lstm_dropout         = float(lstm_drop)
            config.ml.lstm_epochs          = int(lstm_ep)
            config.ml.lstm_learning_rate   = float(lstm_lr)
            config.ml.lstm_batch_size      = int(lstm_bs)

            config.ml.xgb_n_estimators  = int(xgb_n)
            config.ml.xgb_max_depth     = int(xgb_d)
            config.ml.xgb_learning_rate = float(xgb_lr)
            config.ml.xgb_subsample     = float(xgb_sub)
            config.ml.xgb_colsample     = float(xgb_col)

            _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 5 — NEWS & SENTIMENT
# ═════════════════════════════════════════════════════════════════════════════
with tabs[4]:
    st.subheader("News & Sentiment")

    with st.form("form_news"):
        nc1, nc2 = st.columns(2)
        news_lookback = nc1.number_input(
            "Default news lookback (days)", 1, 180, config.alpaca.news_lookback_days,
            help="Used when no days_back is explicitly passed to NewsClient.fetch_news().",
        )
        max_articles = nc2.number_input(
            "Max articles per fetch", 10, 300, config.alpaca.max_articles_per_symbol,
            help="IBKR's totalResults cap is 300.",
        )

        st.divider()
        st.markdown("**FinBERT Sentiment**")

        fc1, fc2 = st.columns(2)
        half_life   = fc1.number_input(
            "Decay half-life (hours)", 1.0, 168.0, config.ml.sentiment_half_life_hours, 1.0,
            help="Older articles are down-weighted exponentially with this half-life.",
        )
        staleness   = fc2.number_input(
            "Staleness window (days)", 1, 30, config.ml.sentiment_staleness_days,
            help="Articles older than this are ignored entirely.",
        )

        st.markdown("**FinBERT availability date** (walk-forward suppression)")
        restrict = st.checkbox(
            "Suppress FinBERT for test windows before a specific date",
            value=config.ml.news_available_from is not None,
        )
        naf_date = None
        if restrict:
            default_date = (
                config.ml.news_available_from.date()
                if config.ml.news_available_from is not None
                else datetime.now().date()
            )
            naf_date = st.date_input(
                "News available from",
                value=default_date,
                help="Walk-forward folds whose test window starts before this date will run with FinBERT score=0.0.",
            )

        finbert_model = st.text_input(
            "FinBERT model (HuggingFace)", value=config.ml.finbert_model_name,
        )

        st.divider()
        st.markdown("**API Keys** — managed via environment variables, not stored here.")
        alpaca_key_set    = bool(config.alpaca.api_key)
        alpaca_secret_set = bool(config.alpaca.secret_key)
        st.info(
            f"ALPACA_API_KEY: {'✓ set' if alpaca_key_set else '✗ not set'}  |  "
            f"ALPACA_SECRET_KEY: {'✓ set' if alpaca_secret_set else '✗ not set'}"
        )

        if st.form_submit_button("Save", type="primary"):
            config.alpaca.news_lookback_days        = int(news_lookback)
            config.alpaca.max_articles_per_symbol   = int(max_articles)
            config.ml.sentiment_half_life_hours     = float(half_life)
            config.ml.sentiment_staleness_days      = int(staleness)
            config.ml.finbert_model_name            = finbert_model
            config.ml.news_available_from = (
                datetime.combine(naf_date, datetime.min.time()) if restrict and naf_date else None
            )
            _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 6 — IBKR CONNECTION
# ═════════════════════════════════════════════════════════════════════════════
with tabs[5]:
    st.subheader("IBKR Connection")

    with st.form("form_ibkr"):
        ic1, ic2 = st.columns(2)
        host        = ic1.text_input("IBKR host", value=config.ibkr.host)
        client_id   = ic2.number_input("Client ID", 1, 999, config.ibkr.client_id, step=1,
                                       help="Must be unique per simultaneous connection.")

        ic3, ic4 = st.columns(2)
        paper_port  = ic3.number_input("Paper trading port", 1024, 65535, config.ibkr.paper_port, step=1)
        live_port   = ic4.number_input("Live trading port",  1024, 65535, config.ibkr.live_port,  step=1)

        ic5, ic6, ic7 = st.columns(3)
        conn_timeout  = ic5.number_input("Connection timeout (s)", 1, 60,  config.ibkr.connection_timeout)
        heartbeat     = ic6.number_input("Heartbeat interval (s)", 5, 300, config.ibkr.heartbeat_interval)
        max_reconnect = ic7.number_input("Max reconnect attempts", 1, 20,  config.ibkr.max_reconnect_attempts)
        reconnect_del = st.number_input("Reconnect delay (s)", 1.0, 60.0, config.ibkr.reconnect_delay, 1.0)

        if st.form_submit_button("Save", type="primary"):
            config.ibkr.host                   = host
            config.ibkr.client_id              = int(client_id)
            config.ibkr.paper_port             = int(paper_port)
            config.ibkr.live_port              = int(live_port)
            config.ibkr.connection_timeout     = int(conn_timeout)
            config.ibkr.heartbeat_interval     = int(heartbeat)
            config.ibkr.max_reconnect_attempts = int(max_reconnect)
            config.ibkr.reconnect_delay        = float(reconnect_del)
            _save_and_notify()


# ═════════════════════════════════════════════════════════════════════════════
# TAB 7 — LOGGING
# ═════════════════════════════════════════════════════════════════════════════
with tabs[6]:
    st.subheader("Logging")

    with st.form("form_logging"):
        lc1, lc2 = st.columns(2)
        log_level = lc1.selectbox(
            "Log level", ["DEBUG", "INFO", "WARNING", "ERROR"],
            index=["DEBUG", "INFO", "WARNING", "ERROR"].index(
                config.logging.level.upper()
            ),
        )
        log_dir = lc2.text_input("Log directory", value=config.logging.log_dir)

        lc3, lc4 = st.columns(2)
        log_to_file    = lc3.checkbox("Log to file",    value=config.logging.log_to_file)
        log_to_console = lc4.checkbox("Log to console", value=config.logging.log_to_console)

        lc5, lc6 = st.columns(2)
        max_mb   = lc5.number_input("Max log file size (MB)", 1, 500, config.logging.max_file_size_mb)
        backups  = lc6.number_input("Backup file count",      1,  20, config.logging.backup_count)

        if st.form_submit_button("Save", type="primary"):
            config.logging.level           = log_level
            config.logging.log_dir         = log_dir
            config.logging.log_to_file     = log_to_file
            config.logging.log_to_console  = log_to_console
            config.logging.max_file_size_mb= int(max_mb)
            config.logging.backup_count    = int(backups)
            _save_and_notify()


# ── Current YAML footer ───────────────────────────────────────────────────────
st.divider()
from config.settings import _YAML_PATH
if _YAML_PATH.exists():
    with st.expander("Current settings.yaml", expanded=False):
        st.code(_YAML_PATH.read_text(encoding="utf-8"), language="yaml")
else:
    st.caption("No `config/settings.yaml` yet — save any section above to create it.")
