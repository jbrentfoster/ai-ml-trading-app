# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-driven algorithmic trading system connecting to Interactive Brokers (IBKR) via IB Gateway (recommended) or TWS. Built as a learning platform with a Streamlit dashboard that explains each component visually. Python async/await throughout for IBKR; all other code is synchronous. Data pipeline uses yfinance → SQLite. Dashboard is Streamlit + Plotly.

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | IBKR connection (paper/live trading) | Complete |
| 2 | yfinance data pipeline + indicators + Streamlit dashboard | Complete |
| 3 | ML signal generation (LSTM, XGBoost, FinBERT ensemble) | Complete |
| 4 | Risk & portfolio management | Complete |
| 5 | RL optimizer (PPO, Sharpe reward) | Pending |
| 6 | Live trading transition | Pending |

## Setup

```bash
pip install -r requirements.txt
```

All commands must be run from the project root (so `config`, `core`, `data`, etc. resolve as packages). The project uses a `.venv` at `trading_app/.venv/`; activate it or prefix commands with `.venv/Scripts/python` on Windows.

Before using IBKR features (IB Gateway recommended over TWS):
- **IB Gateway**: `Configure → Settings → API → Settings` — enable ActiveX and Socket Clients, socket port 4002 (paper) / 4001 (live), uncheck "Read-Only API"
- **TWS**: `File → Global Configuration → API → Settings` — socket port 7497 (paper) / 7496 (live)

## Commands

```bash
# Seed OHLCV, indicators, and news cache for the whole watchlist
python scripts/run_pipeline.py
python scripts/run_pipeline.py --interval 1h
python scripts/run_pipeline.py --skip-news          # skip news fetch + scoring (faster)
python scripts/run_pipeline.py --skip-sentiment     # fetch news but skip FinBERT scoring
python scripts/run_pipeline.py --use-watchlist      # force static watchlist even if universe.enabled=True

# Start the Streamlit dashboard (8-page multi-page app)
streamlit run dashboard/1_Market_Data.py

# Universe selection scheduler
python scripts/universe_scheduler.py               # run forever (manual use only; production uses run_weekly.bat / run_daily.bat)
python scripts/universe_scheduler.py --run-now     # one-shot full refresh then exit
python scripts/universe_scheduler.py --rescore-now # one-shot Stage-3 re-score then exit

# Walk-forward model training (run after run_pipeline.py, before signal_runner.py)
python scripts/train_models.py                    # train all watchlist symbols (full mode)
python scripts/train_models.py --symbol AAPL      # single symbol
python scripts/train_models.py --quick            # faster: 5 epochs / 50 trees / 2 folds
python scripts/train_models.py --force            # retrain even if checkpoints already exist

# Daily signal runner (Phase 4 — risk + order decisions)
python scripts/signal_runner.py                    # dry-run all symbols
python scripts/signal_runner.py --symbol AAPL     # single symbol
python scripts/signal_runner.py --no-dry-run      # submit orders (paper_orders_enabled must be True)
python scripts/signal_runner.py --schedule        # run forever at 09:35 Mon-Fri (manual use only; called automatically by batch files)

# Verify Step 2 data pipeline end-to-end
python scripts/verify_pipeline.py

# Verify Step 3 ML signal layer end-to-end
python scripts/verify_signals.py

# Verify universe selection module
python scripts/verify_universe.py

# Verify risk & portfolio management module
python scripts/verify_risk.py

# Run integration verification against a live paper trading account
python scripts/verify_connection.py

# Test IBKR news API (requires IB Gateway or TWS open)
python scripts/test_ibkr_news.py --symbol AAPL --days 30 --max 300

# Run all tests (no live IB Gateway/TWS or network needed)
.venv/Scripts/pytest tests/ -v

# Run a single test
.venv/Scripts/pytest tests/test_data_pipeline.py::TestComputeIndicators::test_rsi_bounds -v
```

## Complete File Structure

```
trading_app/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── run_daily.bat            — Mon–Sat scheduler: run_pipeline.py → universe_scheduler.py --rescore-now
│                              --no-signal-run → train_models.py (skip-existing) → signal_runner.py;
│                              logs to logs/daily/daily_run_YYYYMMDD.log
├── run_weekly.bat           — Sunday scheduler: run_pipeline.py → train_models.py --force →
│                              universe_scheduler.py --run-now; logs to logs/weekly/weekly_run_YYYYMMDD.log
│
├── scripts/                 — all user-invokable CLI entry points (importable as the `scripts` package).
│   │                          Run any of them from the project root with `python scripts/<name>.py`.
│   ├── __init__.py          — package marker + module docstring
│   ├── run_pipeline.py      — CLI: pre-caches ^VIX, fetches OHLCV + news + FinBERT scoring for all symbols
│   │                          reads active symbols from universe_assets when config.universe.enabled=True
│   │                          (run scripts/universe_scheduler.py --run-now first to populate universe_assets)
│   │                          --use-watchlist forces static watchlist even when universe.enabled=True
│   ├── universe_scheduler.py — Cron-style scheduler: Sunday full refresh + Mon-Sat Stage-3 re-score
│   │                           also runs signal_runner.py --dry-run after each rescore
│   ├── train_models.py      — Walk-forward training for all symbols; saves checkpoints to
│   │                          models/cache/{symbol}/; run between run_pipeline.py and signal_runner.py
│   │                          --symbol, --quick, --force, --interval flags
│   ├── signal_runner.py     — Daily automation: refresh data → generate signals → risk/order decisions
│   │                          5-phase flow; --dry-run (default), --no-dry-run (submit live paper
│   │                          orders — requires IB Gateway + trading.paper_orders_enabled=True),
│   │                          --symbol, --schedule flags.  Bracket orders submitted GTC with prices
│   │                          rounded to $0.01 tick size.
│   ├── open_orders.py       — Ops CLI: list open IBKR orders (default, read-only); add
│   │                          --cancel plus --id / --symbol / --all to cancel
│   ├── open_positions.py    — Ops CLI: list held IBKR positions (default, read-only); add
│   │                          --close plus --symbol / --all (+ optional --qty N for a single
│   │                          symbol) to flatten with market sells
│   ├── verify_pipeline.py   — End-to-end smoke test for Step 2 (data + indicators)
│   ├── verify_signals.py    — End-to-end smoke test for Step 3 (ML signal generation)
│   ├── verify_universe.py   — End-to-end smoke test for universe selection (needs Alpaca keys)
│   ├── verify_risk.py       — End-to-end smoke test for risk & portfolio management (Phase 4)
│   ├── verify_connection.py — Integration test against live IBKR paper account
│   └── test_ibkr_news.py    — Manual test for IBKR reqHistoricalNews API
│
├── config/
│   ├── settings.py          — AppConfig singleton + YAML load/save; see Configuration section
│   └── settings.yaml        — User overrides (auto-created; never stores secrets)
│
├── core/
│   └── logger.py            — get_logger(name): namespaced under "trading.*";
│                              root logger captures WARNING+ from libraries to logs/python/
│
├── execution/
│   └── ibkr_connection.py   — IBKRConnection async context manager; AccountSummary dataclass;
│                              paper + live port switching; all account/order methods are async;
│                              get_last_price(): 3-tier fallback (IBKR live → IBKR 15-min delayed → yfinance);
│                              informational error codes {2104, 2106, 2107, 2119, 2158, 300, 399, 10167, 10197, 10349, 202}
│                              are suppressed from WARNING logs
│
├── data/
│   ├── database.py          — SQLAlchemy ORM + upsert/query helpers; _migrate() for schema
│   │                          includes universe_assets, universe_run_log, circuit_breaker_log,
│   │                          order_decisions, signal_runner_log tables
│   ├── fetcher.py           — DataFetcher: yfinance → incremental SQLite upsert (idempotent)
│   ├── indicators.py        — compute_indicators(df) + IndicatorEngine.run()
│   ├── fundamentals.py      — FundamentalsClient: yfinance with 24h SQLite cache
│   ├── news_client.py       — NewsClient: IBKR → Alpaca → yfinance three-tier fallback
│   ├── universe.py          — UniverseSelector: 3-stage funnel (Alpaca S1 → liquidity S2 →
│   │                          XGBoost/market-cap S3); UniverseRunResult; fixtures always included
│   ├── walk_forward.py      — WalkForwardSplit, WalkForwardValidator, compute_metrics
│   │                          (model-agnostic framework; used by models/walk_forward.py)
│   └── ui_queries.py        — @st.cache_data query functions for all dashboard pages;
│                              query_data_status() (ttl=60) aggregates bar counts, news totals,
│                              fundamentals flag, and model checkpoint status per symbol
│
├── models/
│   ├── base_model.py        — ABC: train / predict / evaluate / save / load + _returns_metrics()
│   ├── lstm_model.py        — 2-layer LSTM (seq=60, hidden=128, tanh); DatasetBuilder;
│   │                          score_series() for per-bar history; weights_only=True checkpoint
│   ├── xgboost_model.py     — XGBoost binary classifier; 12 indicator + 13 fundamental features;
│   │                          output: 2*predict_proba-1 mapped to [-1, 1]
│   ├── finbert_model.py     — ProsusAI/finbert; exponential time-decay aggregation;
│   │                          predict(as_of=) filters to news published <= as_of
│   ├── regime_detector.py   — RegimeType (TRENDING/MEAN_REVERTING/HIGH_VOLATILITY);
│   │                          ADX > 25 -> TRENDING; VIX > 25 -> HIGH_VOLATILITY;
│   │                          ^VIX cached in ohlcv_bars (TTL 4h); inside Streamlit,
│   │                          stale cache is used instead of blocking the UI thread
│   ├── ensemble.py          — Weighted combination; dynamic rebalancing after each WF fold;
│   │                          predict(suppress_finbert=, as_of=) for lookahead prevention
│   ├── signal_gate.py       — SignalResult dataclass; 3-filter gate (see Gate Logic section)
│   └── walk_forward.py      — MLWalkForwardOrchestrator: trains ensemble per fold, bar-by-bar
│                              test window, cost model, finbert_coverage tracking, DB persist
│
├── risk/
│   ├── __init__.py          — exports CircuitBreaker, OrderDecision, OrderManager,
│   │                          GuardResult, PortfolioGuard, PositionSize, PositionSizer,
│   │                          TrailingStopAction, TrailingStopManager
│   ├── position_sizer.py    — fractional Kelly criterion sizing; ATR-based stop/TP;
│   │                          PositionSize dataclass; fallback to fixed 2% stop
│   ├── portfolio_guard.py   — 6-check sequential guard: circuit breaker → drawdown →
│   │                          size → sector → correlation → duplicate; GOOG/GOOGL pair
│   ├── circuit_breaker.py   — SQLite-persisted halt state; auto-reset after N hours;
│   │                          trigger / reset / check_loss_limits / get_status
│   ├── order_manager.py     — full lifecycle: size → guard → DRY_RUN/REJECTED/APPROVED;
│   │                          submits bracket orders via IBKRConnection when enabled
│   └── trailing_stop.py     — TrailingStopManager: walks longs, cancels bracket
│                              TP+STP and submits standalone TRAIL once price has moved
│                              +activation_atr × ATR above entry; persists each
│                              evaluation (CONVERTED/SKIPPED/FAILED) to
│                              trailing_stop_log; opt-in via config.risk.trailing_stop_enabled
│
├── dashboard/
│   ├── 1_Market_Data.py               — Page 1: Market Data & Indicators
│   └── pages/
│       ├── 2_Fundamentals_&_News.py  — Page 2: fundamentals cards + news + sentiment trend
│       ├── 3_Model_Signals.py      — Page 3: score history, signal log, XGBoost importance,
│       │                              LSTM analysis (price/score, heatmap, accuracy)
│       ├── 4_Walk-Forward.py       — Page 4: Sharpe/drawdown/weight charts + results table
│       ├── 5_Settings.py           — Page 5: 7-tab YAML settings editor (includes Risk tab)
│       ├── 6_Data_Status.py        — Page 6: one row per symbol; bar counts, latest timestamps,
│       │                              news/scored counts, fundamentals flag, model flag;
│       │                              amber tint = stale >1d, red tint = no bars; 5 metric cards
│       ├── 7_Universe.py           — Page 7: funnel chart, active table, size history,
│       │                              recently-removed table, manual run controls
│       ├── 8_Risk_&_Portfolio.py     — Page 8: circuit breaker banner, signal runner log
│       │                              (includes Trailing column), order decisions table,
│       │                              trailing stop log (color-coded by action),
│       │                              risk config cards (Kelly/ATR/CB + Trailing section
│       │                              with computed "initial stop at activation" readout),
│       │                              CB event log; sidebar: trigger/reset/run controls
│       ├── 9_Account.py              — Account page: live IBKR account + signal history
│       └── 10_Trade_History.py       — Page 10: closed trades from trade_log (WF-simulated +
│                                      live fills once Phase B lands); summary cards,
│                                      indicative tax-impact view (ST vs LT, configurable
│                                      rates in session_state), color-coded trades table,
│                                      cumulative net-P&L curve + exit-reason donut,
│                                      per-symbol breakdown
│
├── tests/
│   ├── test_data_pipeline.py   — mocked unit tests: fetcher + indicators (patches yfinance + db)
│   ├── test_ibkr_connection.py — mocked unit tests: IBKR connection (patches ib_insync)
│   ├── test_walk_forward.py    — 23 tests: WalkForwardSplit, compute_metrics, orchestrator
│   ├── test_models.py          — 14 tests: LSTM, XGBoost, FinBERT, RegimeDetector, SignalGate
│   ├── test_universe.py        — 15 tests: Stage 1/2/3, fixtures, DB helpers, run result
│   ├── test_risk.py            — 18 tests: PositionSizer (6), PortfolioGuard (6),
│   │                              CircuitBreaker (5), OrderManager (4)
│   ├── test_signal_runner.py   — 6 tests: EQUIVALENT_PAIRS symmetry, within-session
│   │                               deduplication (no-dup, GOOG→GOOGL, GOOGL→GOOG, rejected blocks equiv)
│   └── test_trailing_stop.py   — 8 tests: TrailingStopManager (disabled, idempotent,
│                                   below/above activation, missing ATR, manual position,
│                                   short positions skipped, FAILED path)
│
├── docs/                    — tutorial markdown documents (11 files)
│   ├── README.md            — index with reading order
│   ├── 01-system-overview.md through 10-python-packages.md
│
├── db/
│   └── trading.db           — SQLite (auto-created on first run; gitignored)
├── models/cache/            — saved model weights: {symbol}/lstm.pt, {symbol}/xgb.ubj (gitignored)
└── logs/
    ├── daily/               — daily_run_YYYYMMDD.log (one per run_daily.bat execution)
    ├── weekly/              — weekly_run_YYYYMMDD.log (one per run_weekly.bat execution)
    └── python/
        └── trading_app.log  — rotating Python logger (WARNING+ from all app code)
```


## Data Flow

```
yfinance → DataFetcher.fetch_symbol() → upsert_bars() → SQLite (ohlcv_bars)
                                                             ↓
                                          IndicatorEngine.run() → upsert_indicators()
                                                             ↓ (indicator_snapshots)
NewsClient.fetch_news()      → upsert_news() ──────────> SQLite (news_cache)
  (IBKR → Alpaca → yfinance)
FundamentalsClient.get()     → upsert_fundamentals() → SQLite (fundamental_data)
                                                             ↓
                          MLWalkForwardOrchestrator.run()
                            ├─ per fold: EnsembleModel.train(train_df)
                            ├─ per bar:  EnsembleModel.predict(history_df, as_of=bar_ts)
                            │              └─ LSTM + XGBoost + FinBERT(as_of=bar_ts)
                            ├─ per fold: SignalGate.evaluate() → SignalResult
                            ├─ per fold: EnsembleModel.rebalance(eval_metrics, finbert_coverage)
                            └─ persists: walk_forward_results, ensemble_weight_history
                                                             ↓
                          MLWalkForwardOrchestrator.predict() → signal_log
                                                             ↓
                          dashboard/ (all pages read SQLite only via ui_queries.py)
```

## Database Schema

14 tables in `db/trading.db`. All timestamps are UTC-naive datetimes.

| Table | Key columns | Notes |
|-------|-------------|-------|
| `ohlcv_bars` | symbol, interval, timestamp, OHLCV | unique on (symbol, interval, timestamp); also stores ^VIX |
| `indicator_snapshots` | symbol, interval, timestamp, rsi_14, macd, bb_*, ema_*, atr_14, volume_sma_20 | recomputed from bars by IndicatorEngine |
| `fundamental_data` | symbol, fetched_at, pe_ratio, forward_pe, price_to_book, ev_to_ebitda, revenue_growth, earnings_growth, profit_margin, roe, debt_to_equity, current_ratio, free_cashflow, analyst_target | append-only history (no UNIQUE on symbol — multiple rows per symbol over time); 24h cache in `FundamentalsClient.get` prevents same-day duplicate inserts; readers use `get_fundamentals` (latest row by `fetched_at DESC`) or `get_fundamentals_history` (full series) |
| `news_cache` | symbol, article_id, published_at, headline, sentiment_score | upsert updates score only when stored score is None |
| `signal_log` | symbol, generated_at, bar_timestamp, lstm_score, xgb_score, finbert_score, ensemble_score, regime, signal, passed_gate, gate_reason | written by MLWalkForwardOrchestrator.predict() |
| `ensemble_weight_history` | lstm, xgb, finbert, trigger, recorded_at | written after each rebalance |
| `walk_forward_results` | run_id, symbol, fold_index, train/test dates, sharpe_ratio, max_drawdown, win_rate, n_signals, sentiment_note | sentiment_note added via migration |
| `universe_assets` | symbol PK, name, asset_class, exchange, is_fixture, stage, market_cap, avg_dollar_volume, xgb_score, active, added_at, last_scored_at, removed_at | dynamic universe candidates |
| `universe_run_log` | run_id, run_type, stage, symbol_count, duration_seconds, recorded_at, notes | per-stage timing from universe selector |
| `circuit_breaker_log` | event, reason, daily_loss_pct, weekly_loss_pct, triggered_at, reset_at, recorded_at | TRIGGERED / RESET / AUTO_RESET events |
| `order_decisions` | run_id, symbol, signal, decision, shares, entry/stop/tp prices, position_value, reject_reason, decided_at | per-signal decisions from OrderManager |
| `signal_runner_log` | run_id, run_date, mode, symbols_processed, signals_generated, orders_submitted, orders_rejected, skipped_duplicates, longs_closed, trailing_conversions, duration_seconds | daily run summaries |
| `trailing_stop_log` | run_id, symbol, action, shares, entry_price, current_price, atr, trail_amount, reason, decided_at | one row per position evaluated by TrailingStopManager per run (action ∈ CONVERTED / SKIPPED / FAILED) |
| `trade_log` | source ('walk_forward' \| 'live'), run_id, fold_index, symbol, signal, entry_ts, entry_px, exit_ts, exit_px, exit_reason, shares, pnl, pnl_pct, costs_charged, recorded_at | closed-trade outcomes; populated by MLWalkForwardOrchestrator's bracket simulator (Phase 4.5 — Phase A) and, in future, IBKR fill subscriptions (Phase B). exit_reason ∈ stop / tp / trailing / signal_flip / fold_end / manual_close |

**Schema migrations:** `_migrate()` in `data/database.py` runs at every engine init. When adding a new ORM column, add an `if "column_name" not in cols: ALTER TABLE` block there — never rely on `create_all()` to add columns to existing tables.

## Configuration

Settings load in priority order:
1. Python dataclass defaults (`config/settings.py`)
2. `config/settings.yaml` (user overrides; created/edited by Settings page)
3. Environment variables — secrets only: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`

Secrets are **never** written to YAML. `_SECRET_FIELDS = {"api_key", "secret_key"}` enforces this in `save_yaml_config()`.

Key configurable fields:

| Field | Default | Notes |
|-------|---------|-------|
| `DataConfig.watchlist` | 10 large-caps | Symbols tracked by all pages |
| `DataConfig.daily_lookback_days` | 365 | yfinance fetch window |
| `MLConfig.signal_threshold` | 0.35 | Base gate threshold |
| `MLConfig.signal_lookback_days` | 365 | Default date range on Model Signals page |
| `MLConfig.signal_confirmation` | 2 | Models that must agree (of 3) |
| `MLConfig.ensemble_*_weight` | 0.40/0.35/0.25 | Initial weights (normalised to 1.0 on save) |
| `MLConfig.ensemble_weight_floor` | 0.10 | Minimum per-model weight after rebalance |
| `MLConfig.ensemble_nudge` | 0.10 | Max weight transfer per rebalance |
| `MLConfig.news_available_from` | None | Walk-forward folds before this date suppress FinBERT |
| `MLConfig.wf_train_bars` | 120 | ~6 months daily |
| `MLConfig.wf_test_bars` | 21 | ~1 month daily |
| `MLConfig.wf_n_splits` | 5 | Folds per run |
| `AlpacaConfig.max_articles_per_symbol` | 300 | IBKR hard cap; set this high |
| `AlpacaConfig.news_lookback_days` | 30 | News fetch window |
| `UniverseConfig.enabled` | False | Use dynamic universe instead of static watchlist |
| `UniverseConfig.stage3_max` | 50 | Max candidates after Stage 3 |
| `UniverseConfig.min_market_cap` | $1B | Stage 2 market-cap floor |
| `UniverseConfig.min_avg_dollar_volume` | $5M | Stage 2: (close × volume).mean() over 20 bars |
| `UniverseConfig.permanent_fixtures` | SPY/QQQ/sector ETFs/TLT/GLD/etc. | Always included, bypass all filters |
| `TradingConfig.paper_orders_enabled` | False | When True, submit bracket orders to IBKR paper account in SIMULATION mode |
| `TradingConfig.paper_equity` | $100,000 | Assumed equity when no live IBKR connection is available |
| `TradingConfig.cash_reserve_pct` | 0.20 | Fraction of equity kept in cash; PositionSizer uses `equity × (1 - cash_reserve_pct)` as the investable base |
| `TradingConfig.allow_short_selling` | False | When False (default), SELL signals only close existing long positions — never open shorts; when True, SELL signals may open short positions (future use) |
| `RiskConfig.kelly_fraction` | 0.25 | Fractional Kelly multiplier (quarter-Kelly by default) |
| `RiskConfig.kelly_max_position_pct` | 0.10 | Hard cap on any single position regardless of Kelly output |
| `RiskConfig.atr_stop_multiplier` | 2.0 | Stop = entry ± ATR × multiplier |
| `RiskConfig.atr_take_profit_multiplier` | 3.0 | Take-profit = entry ± ATR × multiplier |
| `RiskConfig.circuit_breaker_daily_loss_pct` | 0.03 | 3% single-day loss triggers trading halt |
| `RiskConfig.circuit_breaker_weekly_loss_pct` | 0.07 | 7% weekly loss triggers trading halt |
| `RiskConfig.circuit_breaker_reset_hours` | 24 | Auto-reset halt after N hours |
| `RiskConfig.max_sector_exposure_pct` | 0.30 | 30% cap on total equity in any one sector |
| `RiskConfig.max_correlated_positions` | 3 | Max positions with Pearson r ≥ correlation_threshold |
| `RiskConfig.trailing_stop_enabled` | False | When True + paper_orders_enabled, convert winning bracket TPs into GTC TRAIL orders |
| `RiskConfig.trailing_stop_activation_atr` | 2.0 | Convert once current price ≥ entry + N × ATR. Default matches trail distance so the initial trailing stop lands at break-even |
| `RiskConfig.trailing_stop_trail_atr` | 2.0 | Trail distance in ATR multiples (typically matches atr_stop_multiplier) |

## ML Model Details

### LSTM (`models/lstm_model.py`)
- Architecture: 2-layer LSTM, hidden=128, dropout=0.2, tanh output → score in [-1, 1]
- Input: rolling window of `seq_len=60` bars × 17 features (OHLCV + RSI + MACD + BB + EMA + ATR + VolSMA)
- Target: sign of 5-bar forward return (+1 up, -1 down)
- Normalisation computed from training window only (no lookahead)
- `score_series(df)` returns per-bar scores for the full df (NaN for first 60 bars) — used by the LSTM Analysis charts on Page 3
- Checkpoint: stores `mean`/`std` as torch tensors + feature list as plain Python list → safe with `weights_only=True` (PyTorch ≥ 2.6). Falls back to `weights_only=False` for old checkpoints with a warning.

### XGBoost (`models/xgboost_model.py`)
- 12 indicator features + 13 fundamental features = 25 total
- Binary classification (up/down); output mapped: `2 * predict_proba - 1` → [-1, 1]
- Fundamentals fetched from `FundamentalsClient` (cached in SQLite, 24h TTL)
- Saved as `.ubj` (binary JSON, XGBoost native format)

### FinBERT (`models/finbert_model.py`)
- Model: `ProsusAI/finbert` (downloads ~400 MB on first use)
- Aggregation: exponential time-decay weighted average over recent news; articles older than `sentiment_staleness_days` (default 7) contribute 0
- `predict(df, symbol, as_of=None)`: when `as_of` is set, only uses articles with `published_at <= as_of` and skips live fetch — prevents lookahead during walk-forward
- Dow Jones prefix `{A:800015:L:en}` is stripped from IBKR headlines before scoring
- `train()` is a no-op (pre-trained model, no fine-tuning)

### Ensemble (`models/ensemble.py`)
- Weighted sum: `w_lstm * lstm + w_xgb * xgb + w_finbert * finbert`, clipped to [-1, 1]
- `suppress_finbert=True`: FinBERT score forced to 0.0; its weight split equally to LSTM/XGBoost. Used for walk-forward windows predating `news_available_from`.
- `rebalance(eval_results, finbert_coverage)` after each fold:
  1. LSTM vs XGBoost: nudge weight toward whichever had higher Sharpe (FinBERT excluded — its evaluate() is a stub)
  2. FinBERT: `weight = configured_base_weight × finbert_coverage` (always from configured baseline, never drifts)
  3. Apply floor (10%) then normalise so weights sum to 1.0

### Signal Gate (`models/signal_gate.py`)
Three sequential filters — all must pass for BUY/SELL:
1. **Threshold**: `|ensemble_score| >= signal_threshold` (default 0.35)
2. **Regime-adjusted threshold**: HIGH_VOLATILITY raises threshold ×1.5; TRENDING lowers ×0.9; MEAN_REVERTING unchanged
3. **Model confirmation**: ≥ `signal_confirmation` (default 2) of 3 models must agree on direction

### Regime Detector (`models/regime_detector.py`)
- ADX > 25 → TRENDING
- VIX > 25 → HIGH_VOLATILITY (VIX fetched from yfinance, cached as `^VIX` in ohlcv_bars, TTL 4h)
- Otherwise → MEAN_REVERTING

### Walk-Forward Framework
Two-layer architecture:
- `data/walk_forward.py`: model-agnostic `WalkForwardSplit` + `WalkForwardValidator` + `compute_metrics`. Can be used independently with any strategy function.
- `models/walk_forward.py`: `MLWalkForwardOrchestrator` — ML-specific wrapper that trains the ensemble, runs bar-by-bar test windows (passing `bar_ts` as `as_of` to FinBERT), tracks `finbert_coverage`, rebalances weights, and persists results to SQLite.

Walk-forward default config (full mode): 120 train bars + 1 gap + 5 × 21 test bars = 226 bars minimum.
Quick mode (UI): 5 LSTM epochs / 50 XGB estimators / 2 folds / 60 train + 10 test = 81 bars minimum.

## News & FinBERT Conventions

- **News must be fetched before walk-forward** for FinBERT to contribute anything. Run `python scripts/run_pipeline.py` or use "Fetch & Score News" on Page 2 for each symbol.
- IBKR news goes back ~4-5 months (300 article hard cap). Set `MLConfig.news_available_from` to the earliest reliable date so walk-forward folds before it suppress FinBERT explicitly and log the reason in `sentiment_note`.
- `upsert_news` updates `sentiment_score` only when the stored value is `None` — it never overwrites a previously scored article.
- FinBERT coverage (fraction of test-window bars with non-zero score) is tracked per fold and stored in `sentiment_note`. Coverage < 100% scales FinBERT's weight proportionally.

## Dashboard Conventions

All 6 pages follow the same patterns:

- **Chart style**: `template="plotly_dark"`, teal `#26a69a` for bullish/positive, red `#ef5350` for bearish/negative, `margin=dict(l=0, r=0, t=40, b=0)`
- **Data access**: all pages query SQLite via `data/ui_queries.py` functions decorated with `@st.cache_data(ttl=300)`. Pages never hit yfinance or the network directly (except Page 2's "Fetch & Score News" button and Page 6's "Refresh from TWS").
- **Educational captions**: every chart has a `st.caption()` below (or `st.markdown()` + `st.caption()` before/after) explaining what the chart shows, how to read it, and how it connects to the trading logic.
- **Empty states**: every section has an `st.info()` message when data is absent, explaining what to run to populate it.
- **Sidebar controls**: date range pickers use `config.ml.signal_lookback_days` (default 365) as the default lookback on Page 3.
- **Cache clearing**: sidebar "Refresh cache" buttons call `.clear()` on the relevant `query_*` functions then `st.rerun()`.

Dashboard pages:
- **Page 1** (`1_Market_Data.py`): Candlestick + RSI/MACD/ATR 4-panel chart, Bollinger Bands + EMA overlays, volume chart, OHLCV+indicator data table with CSV export, watchlist summary expander
- **Page 2** (`2_Fundamentals_&_News.py`): fundamental metric cards, key ratios bar chart, growth/profitability bar chart, rolling 7-day sentiment trend, color-coded news headlines table
- **Page 3** (`3_Model_Signals.py`): latest score metrics + regime badge + weight donut, score history line chart, signal log table, XGBoost feature importance bar chart, LSTM Analysis section (price+score panel, 60-bar input heatmap, directional accuracy chart)
- **Page 4** (`4_Walk-Forward.py`): summary metric cards, Sharpe bar chart per fold, max drawdown line chart, ensemble weight evolution stacked area, detailed results table with sentiment_note column
- **Page 5** (`5_Settings.py`): 7-tab YAML editor (Watchlist & Data / Universe / Trading / ML Models / News & Sentiment / IBKR Connection / Logging); saves to `config/settings.yaml`; secrets never shown or written
- **Page 6** (`6_Data_Status.py`): one row per symbol — bar counts (daily/hourly), latest bar timestamps + age, news/scored article counts, fundamentals flag, model checkpoint flag; 5 summary metric cards; amber row = stale >1 day, red row = no bars at all; sidebar Refresh button
- **Page 7** (`7_Universe.py`): funnel overview, active candidates table, size history, recently removed, run log, manual refresh buttons
- **Page 8** (`8_Risk_&_Portfolio.py`): circuit breaker status banner, signal runner log, order decisions (color-coded), risk config cards + Kelly explainer, CB event log; sidebar: trigger/reset/run controls
- **Page 9** (`9_Account.py`): live IBKR account summary + positions (enriched with risk levels + live yfinance prices) + open orders; position allocation donut. TWS required — no signal/trade history (Page 3 covers signals, Page 8 covers order decisions, Page 10 covers closed trades)
- **Page 10** (`10_Trade_History.py`): closed trades from `trade_log`; 5 summary cards (gross/net P&L, fees, win rate); indicative tax view (ST vs LT split, configurable rates in `st.session_state`, *not* tax advice); color-coded trades table with CSV export; cumulative net-P&L curve (gross vs net) + exit-reason donut; per-symbol breakdown expander

## Key Architectural Decisions

**SQLite over Postgres**: Simplicity — no server to run, single-file database, sufficient for a single-user learning tool. All timestamps stored UTC-naive to avoid SQLAlchemy timezone complexities.

**`_migrate()` pattern over Alembic**: Alembic is heavyweight for a project at this stage. `_migrate()` with idempotent `ALTER TABLE` checks runs automatically at engine init and handles the only common DDL operation (adding columns). Adding a new column = add one `if "col" not in cols:` block.

**`as_of` threading through Ensemble → FinBERT**: The walk-forward orchestrator passes each bar's timestamp as `as_of` so FinBERT only uses news published on or before that date. Without this, FinBERT would aggregate today's news for every historical bar — a severe lookahead bias. The `as_of` parameter also skips the live API fetch for historical bars (no point fetching; cached articles are filtered in-memory instead).

**Coverage-based FinBERT weighting**: FinBERT's `evaluate()` can't produce a Sharpe ratio (sentiment can't be backtested like a price model), so it's excluded from the LSTM/XGBoost Sharpe competition. Instead, its weight scales with `finbert_coverage = non-zero bars / total bars` in each test window, reset from the configured baseline each fold (no drift). This ensures sparse-news folds automatically reduce FinBERT's influence.

**asyncio event loop before ib_insync import**: `ib_insync` depends on `eventkit`, which calls `asyncio.get_event_loop()` at import time. On Python 3.10+ in non-main threads (Streamlit's ScriptRunner), this raises. The fix is to create and set a new event loop *before* the `from ib_insync import ...` statement in `NewsClient._fetch_from_ibkr_standalone()` and `9_Account.py`.

**LSTM checkpoint format (torch tensors)**: PyTorch ≥ 2.6 changed `weights_only` default to `True`. Storing `pd.Series` in checkpoints breaks this (unsafe type). Fix: store `mean`/`std` as `torch.tensor(series.values)` and feature names as a plain `list`. Load converts tensors back to Series. Old checkpoints fall back to `weights_only=False` with a warning — retrain to upgrade.

**Three-tier news fallback (IBKR → Alpaca → yfinance)**: IBKR provides the best-quality financial news (~4-5 months back, 300 articles max) but requires IB Gateway or TWS. Alpaca is the secondary source (requires free API key). yfinance is the always-available fallback. The tiering means the system degrades gracefully without any API keys configured.

**`upsert_news` never overwrites scores**: Scoring is expensive (FinBERT ~400MB model). Once an article has a score it's permanent. The upsert only fills in `sentiment_score` when it's currently `None`. This lets `run_pipeline.py` be re-run idempotently — only new unscored articles are processed.

**`data/walk_forward.py` vs `models/walk_forward.py`**: Two layers intentionally. `data/walk_forward.py` is model-agnostic and usable with any strategy function (useful for Phase 4/5 development). `models/walk_forward.py` is the ML-specific orchestrator that wires the ensemble, signal gate, cost model, and DB persistence together.

**`score_series()` on LSTMModel**: Added to support the LSTM Analysis charts (Page 3). Runs inference bar-by-bar over a full DataFrame — first `seq_len` bars return NaN (no complete window). Returns a Series with the same DatetimeIndex as `df` for direct chart overlay.

**Universe survivorship bias**: When `MLWalkForwardOrchestrator` is constructed with `universe_selector`, it logs a warning that walk-forward results may be biased — the universe was determined using today's data, so historical folds can include symbols that were only selected in hindsight. For unbiased backtests, use the static watchlist (`--use-watchlist` flag or leave `config.universe.enabled=False`).

**Stage 3 pre-scoring OHLCV backfill**: `_stage3_score` in `data/universe.py` fetches bars via `DataFetcher.fetch_symbol(days_back=365)` for any Stage 2 survivor missing OHLCV before running the XGBoost ranker. Without the backfill, `IndicatorEngine.run()` returns an empty DataFrame → `xgb_score=0.0` → the symbol is ranked last → the universe calcifies around previously-tracked symbols (observed: 2026-04-19 weekly run had 249/300 candidates zero-scored). Cost: ~5-10 min of yfinance calls per full refresh, added to the existing Stage 2 duration (~70 min). Scored / zero-score counts are logged for visibility.

**Risk module dry-run default**: `signal_runner.py` defaults to `dry_run=True` (argparse `default=True`). To actually submit orders, pass `--no-dry-run` AND set `paper_orders_enabled=True` in config (for SIMULATION mode) or switch to LIVE mode. This two-gate approach prevents accidental order submission.

**Within-session deduplication (`EQUIVALENT_PAIRS`)**: `signal_runner.py` defines `EQUIVALENT_PAIRS = {"GOOG": "GOOGL", "GOOGL": "GOOG"}`. Phase 4 tracks `decided_symbols` across the run; if a symbol's equivalent has already been decided, the second symbol is skipped (no `OrderManager.process()` call, no DB record written). The `skipped_duplicates` count is logged in Phase 5 and persisted to `signal_runner_log`. `PortfolioGuard`'s built-in GOOG/GOOGL check only fires against live IBKR positions; this within-session guard is needed because `positions={}` in dry-run mode means the guard sees no existing positions to compare against.

**Long-only SELL handling (`allow_short_selling=False`)**: `OrderManager.process()` intercepts SELL signals before position sizing and the portfolio guard when `config.trading.allow_short_selling=False` (the default). If an existing long is found in `positions` → `_close_long_position()` is called (market sell to flatten the position) and `decision='CLOSED_LONG'` is returned. If no long is held → `decision='REJECTED_NO_POSITION'` is returned with no order placed. The guard and position sizer are bypassed entirely for close orders (closing reduces risk; no new sizing is needed). `longs_closed` is tracked in Phase 4 and persisted in `signal_runner_log`. Page 8 shows CLOSED_LONG in purple and REJECTED_NO_POSITION in amber. When `allow_short_selling=True`, the normal bracket-order path runs for SELL signals (future use — currently unreachable in practice). The walk-forward bracket simulator (`models/walk_forward.py:_run_test_window`) reads the same flag and applies the same rule: a SELL from flat is a no-op, a SELL after a long is a close-only signal_flip with no short scheduled. Without this gate, WF aggregate P&L was systematically misleading — 2026-04-30 audit found 66% of simulated trades were short opens that the live runner would never have executed; the long-only subset had +0.06 Kelly while the combined aggregate had −0.08 Kelly.

**Stop-price sanity check in PortfolioGuard**: Check #2 of the 7-check sequential guard (between `circuit_breaker` and `portfolio_drawdown`) verifies the stop price sits on the loss side of the entry price for the given signal — BUY requires `stop < entry`, SELL requires `stop > entry`. Guards against a bad ATR (NaN → 0 → `stop == entry`) or a sign-flip in stop placement, either of which would turn the safety stop into an instant or inverse-direction trigger. Also rejects `entry <= 0` and `stop <= 0`.

**`REJECTED_TOO_SMALL` short-circuits the order flow**: `OrderManager.process()` checks `pos_size.shares < 1` immediately after `PositionSizer.calculate()` and returns `decision='REJECTED_TOO_SMALL'` without calling the PortfolioGuard or submitting any IBKR order. This covers two scenarios: (a) Kelly/fixed sizing produced `position_value < entry_price` (tiny allocation on a high-priced stock); (b) `_get_latest_close()` returned 0 because no bars were cached for the symbol. Without this check, a 0-share "APPROVED" decision would be written to `order_decisions` and (in live mode) IBKR would either reject or silently no-op a 0-share bracket order.

**Circuit breaker is shared state**: `CircuitBreaker` reads/writes the `circuit_breaker_log` table in the shared SQLite DB. The dashboard (Page 8), `signal_runner.py`, and `universe_scheduler.py` all share the same state. The short `ttl=30` on `query_circuit_breaker_status()` means the dashboard reflects reality within 30 seconds without manual refresh.

**PortfolioGuard sector check is best-effort**: Sector exposure blocking only works for symbols in the hardcoded `_SECTOR_MAP` dict in `risk/portfolio_guard.py`. Unknown symbols (most mid/small-caps) pass through without a sector check. To extend coverage, add entries to `_SECTOR_MAP`.

**Universe Stage 1 requires Alpaca API keys**: `UniverseSelector._stage1_fetch()` calls `TradingClient.get_all_assets()`. Without keys it raises `UniverseError`. Permanent fixtures are always added regardless, so `run_full()` with no keys will produce a fixture-only list rather than crashing.

**Batch files over persistent scheduler**: Production automation uses `run_daily.bat` (Mon–Sat 09:40) and `run_weekly.bat` (Sunday 01:00) driven by Windows Task Scheduler, not a persistent `universe_scheduler.py --forever` process. Each batch file runs steps sequentially and exits — this avoids silent failures from processes dying overnight, multiple instances on re-login, and race conditions between pipeline and training. Daily training skips existing checkpoints (no-op after first run); weekly uses `--force` for full retraining. `set PYTHONUTF8=1` is set in both batch files to handle Unicode in log output on Windows.

**`get_last_price()` 3-tier market data fallback**: `IBKRConnection.get_last_price()` tries: (1) `reqMarketDataType(1)` live snapshot — requires real-time API subscription in IBKR Client Portal; (2) `reqMarketDataType(3)` 15-minute delayed data — free, no subscription needed, uses `snapshot=False` (required for delayed); (3) yfinance `fast_info.last_price` — always available. Error 10089 (no real-time subscription) is expected without a subscription and triggers automatic fallback to delayed data.

**Phase-4 live-order wiring (`--no-dry-run`)**: `signal_runner._phase4_risk_orders` opens a single event loop and a single `IBKRConnection` at the top of the phase, reuses both for every `OrderManager.process()` call, and closes them in a `finally` block. The loop is set as current via `asyncio.set_event_loop(loop)` **before** `IBKRConnection()` is instantiated — `ib_insync` calls `asyncio.get_event_loop()` during `IB()` construction and raises on Python 3.13 if no loop is bound to the thread. `OrderManager` now accepts an `event_loop` parameter so `_submit_bracket_order` / `_submit_market_close` reuse the same loop instead of creating a fresh one per call. If IBKR is unreachable mid-phase, the runner prints `⚠ IBKR unreachable — falling back to dry-run for this phase` and continues with `dry_run=True`.

**Bracket orders use GTC + tick-rounded prices**: `IBKRConnection.place_bracket_order` applies two fixes that keep brackets alive end-to-end: (1) `round(price, 2)` on entry / stop / TP to satisfy IBKR's minimum-tick-variation check (error 110 — `ib_insync` passes prices through float32 on the wire, which drifts e.g. 202.52 → 202.52000427246094); (2) `leg.tif = "GTC"` on every leg so the bracket survives if the runner fires outside RTH (DAY-TIF orders are immediately cancelled after the 16:00 ET close, which is why error 10349 "Order TIF was set to DAY based on order preset" lost the LMT legs before GTC was added). The $0.01 tick size is correct for all US equities on the current watchlist; sub-penny stocks would need a per-contract tick lookup via `reqContractDetails`.

**STP trigger price lives on `auxPrice`, not `lmtPrice`**: IBKR stores stop-trigger prices in the `auxPrice` field on STP / STP LMT orders; `lmtPrice` is only populated for LMT / STP LMT legs. `OrderResult` has both `limit_price` and `stop_price` fields, and `__str__` renders stops as `@ stop $191.92`. `IBKRConnection.get_open_orders()` returns both. `ib_insync` fills unused price fields with `sys.float_info.max` (~1.8e308) rather than `None` — the `_clean_price()` helper in both `place_bracket_order` and `get_open_orders` treats any non-finite value, anything `> 1e100`, or exact zero as "no price" and returns `None`. Without that sanitisation the Account page renders `$nan` for STP rows.

**Informational error codes continue to expand**: The `informational` set in `IBKRConnection._on_error` now includes `202` (Order Canceled confirmation — logged at ERROR by ib_insync but it's just the ack for a successful `cancelOrder`). Code 10349 was also added ("Order TIF was set to DAY based on order preset") — IBKR's preset may rewrite TIF even when the client sets GTC explicitly. Full current set: `{2104, 2106, 2107, 2119, 2158, 300, 399, 10167, 10197, 10349, 202}`.

**Trailing stops run as Phase 3.5, after signal generation and before new-order submission** (`scripts/signal_runner.py`, `risk/trailing_stop.py`). `TrailingStopManager.manage()` walks every long IBKR position, finds its bracket's LMT take-profit and STP stop-loss legs in `get_open_orders()`, fetches `atr_14` from `indicator_snapshots` and the latest daily close from `ohlcv_bars`, and when `current_price ≥ entry + activation_atr × ATR` performs Cancel-TP → Cancel-STP → Submit-TRAIL. The conversion order is intentional: the alternative (submit TRAIL first, cancel bracket legs after) would create a multi-second window in which STP and TRAIL are both live in different OCA groups, so a severe gap-down could trigger both and leave the account short by the position size. The current order leaves a sub-second "no stop" window instead — acceptable in normal markets. Idempotent: positions with an existing TRAIL order are skipped. Phase 3.5 is skipped entirely in dry-run mode, when `paper_orders_enabled=False`, or when `trailing_stop_enabled=False` (the default — opt-in feature). Standalone TRAIL orders use `orderType="TRAIL"` with `auxPrice=trail_amount` (rounded to $0.01 to satisfy IBKR minimum-tick-variation, same as bracket prices) and `tif="GTC"`. `OrderResult.__str__` renders TRAIL as `@ trail $X` using the `stop_price` field (which carries `auxPrice` — the trailing distance, not a trigger level). Short positions are skipped (long-only codepath); once `allow_short_selling=True` is wired, a symmetric BUY trailing stop path would be needed.

## Logging conventions

`core/logger.py` sets up a single `RotatingFileHandler` writing to `logs/python/trading_app.log` (50 MB × 5 backups). All `trading.*` loggers default to INFO; the root logger captures WARNING+ from libraries. The daily/weekly batch files (`run_daily.bat`, `run_weekly.bat`) redirect stdout+stderr into `logs/daily/` and `logs/weekly/`, so `print()` output and `log.*` output interleave there.

**Level policy** — every line should justify its level:

| Level | Use for | Examples |
|-------|---------|----------|
| `DEBUG` | Routine no-ops, framework chatter, "I did the boring thing" | "Stored 0 new bar(s) — already current", "LSTM loaded from path", "Fetching AAPL", "Fetched and cached fundamentals" |
| `INFO` | Real events, decisions, state changes the operator would want to see in tomorrow's log | "Stored 12 new bar(s)", "Bracket order submitted", "[ABT] REJECTED — Already holding GLD", "Circuit breaker auto-reset" |
| `WARNING` | Recoverable problem or degraded path | "yfinance returned no data", "Correlation check skipped", "IBKR connection lost" |
| `ERROR` | Broken state requiring action | "Bracket order submission failed", "Submit TRAIL failed — position is now UNPROTECTED" |

**Convention for "stored N rows" patterns**: `n > 0` → INFO (real activity), `n == 0` → DEBUG (no-op). Used in `data/fetcher.py:fetch_symbol` and `data/indicators.py:run`. Do not log "Stored 0 new bar(s)" at INFO — it produces 68× steady-state noise per pipeline run with zero diagnostic value. To re-enable the per-symbol detail temporarily, set `LoggingConfig.level: DEBUG` in `config/settings.yaml`.

**Demoted-to-DEBUG checkpoint loaders**: `LSTM loaded from ...`, `XGBoost loaded from ...`, `Ensemble models loaded from ...`, `Fetching <sym> | interval=...`, `Fetched and cached fundamentals for ...`. These are loaded once per symbol per signal-runner pass and produce ~5 lines × 68 symbols = 340 lines of pure framework chatter at INFO. They remain at INFO during *training* (`LSTM training complete`, `Ensemble training XGBoost ...`) because that's a real event you want to see.

**HuggingFace silencing** (`models/finbert_model.py` top of file): three env vars are set at module import — `TRANSFORMERS_VERBOSITY=error`, `HF_HUB_DISABLE_PROGRESS_BARS=1`, `HF_HUB_DISABLE_TELEMETRY=1` — plus a runtime call to `transformers.logging.set_verbosity_error()` inside `_get_pipeline()`. Without these, every FinBERT load dumps a tqdm `Loading weights:` progress bar, a `BertForSequenceClassification LOAD REPORT` verbose model-load summary, and a `You are sending unauthenticated requests to the HF Hub` warning into the daily log. The env vars must be set **before** `transformers` or `huggingface_hub` are imported anywhere in the process — that's why they live at the top of `finbert_model.py`, the only file that imports `transformers`. Do not move them.

**Audit history**: a logging audit on 2026-04-30 produced a punch list of trim-noise items (#1, #4) that landed in this commit, plus three structured-detail items (#2, #3, #5) deferred to a future session — see *Logging quality v2* in the Enhancements list below for the carry-over. A separate weekly-log trim is also pending the next Sunday baseline — see *Weekly log trim* in Enhancements.

## Known Issues & TODOs

*Last cleanup: 2026-04-18*

### Convention: documenting fixed bugs

**When you fix a bug from any list below, do not delete the entry — annotate it.** Bugs in these lists are tracked across sessions by future agents who lack context from the fix conversation. A naked deletion (or a "fixed!" with no implementation pointer) leaves the next agent unable to verify the work or distinguish "shipped and verified" from "shipped but never run live."

Required annotation when a fix lands in code:

1. **Append a status marker to the bug title** in italics:
   `*(code complete YYYY-MM-DD — awaiting live verification)*`
2. **Preserve the original problem description** (the WHY). Do not rewrite it.
3. **Append a `**Status:**` paragraph** at the end of the entry covering:
   - **What was implemented** — name the new files/functions/tables/config fields. Future agents grep for these names.
   - **Test coverage added** — count + file (e.g. "5 new in `test_signal_runner.py`"). If no tests were added, say so and explain why (e.g. "no unit-testable surface — covered by integration only").
   - **Verification pending** — what *observable behaviour* in the next live run would confirm the fix actually works. Be specific: "needs ≥2 paper-trading runs to confirm daily-pct math" beats "needs testing."

Once a fix is verified live (the user confirms it worked end-to-end in production), move the entry into the changelog/history record (when one exists) or delete it from this list. Until then, it stays here under the "code complete" marker so future agents know it's a closed loop awaiting a real-world signal.

### Outstanding bugs (from 2026-04-18 audit — pending fix)

**Correctness — high priority:**
- **Circuit breaker is effectively manual-only** *(code complete 2026-04-28 — awaiting live verification)* (`risk/circuit_breaker.py`, `signal_runner.py`): `check_loss_limits(daily_pct, weekly_pct)` requires the caller to pass realised loss percentages, but nothing in the codebase computes them. `signal_runner._phase1_startup` should pull `realized_pnl + unrealized_pnl` from `IBKRConnection.get_account_summary()` (and a cached equity baseline) and invoke the check before any signals are processed. Without this, the CB only trips when a human clicks "Trigger" on Page 8. **Status:** implemented via new `equity_snapshots` table + `_check_loss_limits_against_baseline` helper called from Phase 1 when not dry-run. Unit tests pass (5 new in `test_signal_runner.py`). Verification pending: first live `--no-dry-run` run only seeds the baseline (no comparison yet), so the auto-trigger genuinely activates from run #2 onward — needs ≥2 consecutive paper-trading runs to confirm the daily-pct math, plus a deliberately-induced loss to exercise the actual halt path end-to-end.
- **Kelly disconnected from realised outcomes** (`risk/position_sizer.py`): Kelly fraction is derived from `|ensemble_score|` as a P(win) proxy; the entire risk stack never sees actual fills or P&L. Fix: introduce a `trade_log` table (symbol, entry_ts, exit_ts, entry_px, exit_px, pnl, pnl_pct, signal) populated from IBKR fills, then compute Kelly from a rolling empirical win-rate + avg win / avg loss. Also unlocks Phase 5 RL rewards and real performance attribution. **→ rolled into "Phase 4.5 — Realised P&L plumbing" below; design resolved, awaiting code.**
- **Stale-price signals accepted** *(code complete 2026-04-28 — awaiting live verification)* (`signal_runner.py` + `risk/order_manager._get_latest_close`): `get_bars(..., limit=1)` returns the most recent cached bar regardless of age. If the pipeline hasn't run for a week, signals fire against week-old prices. Fix: gate each symbol on `latest_bar_age < max_stale_days` (config) before passing to `OrderManager.process`. **Status:** implemented via new `RiskConfig.max_bar_staleness_days` (default 3) + a stale-bar gate at the top of `_phase3_signals` that drops symbols whose newest cached daily bar is older than the limit. `skipped_stale` count surfaces in Phase 5 print + `signal_runner_log.skipped_stale`. Unit tests pass (2 new in `test_signal_runner.py`: fresh-pass and stale-drop). Verification pending: needs a real-world run after a deliberate pipeline-skip (e.g. one weekend without `run_pipeline.py`) to confirm the gate fires correctly against actual stale data and that the dashboard shows the skipped count.
- **FinBERT weight floor defeats coverage scaling** *(code complete 2026-05-01 — awaiting live verification)* (`models/ensemble.py` rebalance): after multiplying FinBERT weight by `finbert_coverage`, the 10 % `ensemble_weight_floor` is reapplied unconditionally. A symbol with 0 % coverage still ends up with ≥ 10 % FinBERT weight. Fix: skip the floor when `finbert_coverage == 0`, or apply the floor only to LSTM/XGBoost. **Status:** `_normalise_weights` now floors only LSTM and XGBoost; FinBERT weight passes through untouched so the coverage scaling in `rebalance` step 2 governs it across the entire range (not just at coverage=0). No new tests — the existing `test_models.py` ensemble suite covers the rebalance/normalise flow and continues to pass (146/146). Verification pending: next weekly `--force` retrain — observable in `ensemble_weight_history`, where a 0-coverage symbol's row should now show `finbert ≈ 0` (post-normalisation, ratios depend on LSTM/XGB) instead of `finbert ≥ 0.10`. Low-coverage symbols (e.g. coverage=0.20) should also show proportionally lower FinBERT weight than before.
- **FinBERT `published_at` type assumption** (`models/finbert_model.py:123`): `[a for a in articles if a["published_at"] <= now]` assumes every source returns a `datetime`. ORM reads do, but `NewsClient` has three fallback providers (IBKR / Alpaca / yfinance) — confirm each hands off a `datetime` before reaching this filter, or coerce with `pd.to_datetime(a["published_at"])` to be safe. Silent wrong comparison under Py3 is the concern; TypeError is possible if any provider returns a string.
- **Non-Wilder ADX** (`data/indicators.py`): ADX uses a simple rolling mean instead of Wilder's smoothing (EMA with α = 1/n). This biases the regime detector's TRENDING threshold. Fix: switch to Wilder smoothing, or lower the ADX cutoff to compensate.
- **No HOLD-timeout exit rule**: once a BUY fills, the position sits until an explicit SELL signal fires. In sparse-signal regimes a position can hold indefinitely. Fix: add a config-driven "flatten after N bars without a re-confirming signal" rule in `signal_runner.py`, or a time-based stop alongside the ATR stop.

**Performance / hygiene — lower priority:**
- **Row-by-row upserts** in `data/database.py` helpers (`upsert_bars`, `upsert_indicators`, `upsert_news`): each row is a separate transaction. For a 365-bar backfill × 10 symbols this is ~3500 round-trips. Fix: switch to `INSERT ... ON CONFLICT DO UPDATE` with `executemany` or SQLAlchemy's bulk upsert.
- **Deprecated `datetime.utcnow`** *(code complete 2026-05-01 — awaiting live verification)* (`data/database.py:65, 97`): emits DeprecationWarning on Python 3.12+. Fix: `datetime.now(timezone.utc).replace(tzinfo=None)` (matches the UTC-naive convention already used elsewhere). **Status:** added a module-level `_utc_now()` helper that returns a UTC-naive `datetime`; both `OHLCVBar.created_at` and `IndicatorSnapshot.created_at` now use `default=_utc_now`. No new tests (defaults are exercised by every existing insert path; the suite passes 146/146). Verification pending: next pipeline run should produce no `DeprecationWarning: datetime.datetime.utcnow() is deprecated` output in stderr.
- **Wrong dashboard path in stdout** *(code complete 2026-05-01 — awaiting live verification)* (`run_pipeline.py:91, 155`, `dashboard/1_Market_Data.py:8` docstring): print `streamlit run dashboard/app.py` but the real entry point is `dashboard/1_Market_Data.py`. Fix: update the strings. **Status:** updated both `run_pipeline.py` print sites, `dashboard/1_Market_Data.py:8` docstring, and the `.streamlit/config.toml` comment that referenced the same wrong path. Verification pending: trivial — the next `python scripts/run_pipeline.py` run should print the correct command at completion.
- **Non-deterministic LSTM training** *(code complete 2026-05-01 — awaiting live verification)* (`models/lstm_model.py`): no `torch.manual_seed`/numpy seed set, so walk-forward folds vary run-to-run. Fix: seed in `train()` before the epoch loop (keep configurable). **Status:** new `MLConfig.lstm_random_seed: int | None = 42` (set to `None` for non-deterministic training); `LSTMModel.__init__` reads it into `self._seed`, and `train()` calls `torch.manual_seed` + `np.random.seed` before the dataset/network are constructed. No new tests — determinism is observable in the WF output, not a unit-test surface. Verification pending: two back-to-back `python scripts/train_models.py --quick --symbol AAPL --force` runs should produce identical fold Sharpe ratios in `walk_forward_results`. Once confirmed, the same property holds for the full Sunday `run_weekly.bat` retrains and makes WF results comparable across config tweaks.
- **Event loop per order call** (`risk/order_manager.py`): `_submit_bracket_order` / `_submit_market_close` each create and close a fresh event loop. Fine in single-threaded `signal_runner.py` but risks `RuntimeError: Event loop is closed` from lingering ib_insync callbacks. Related: `signal_runner.py` could be moved to async and reuse a single `IBKRConnection` for the whole run instead of opening/closing per order.
- **Sharpe annualisation hardcoded** (`data/walk_forward.py` `compute_metrics`): uses `√252` regardless of bar interval. Fine for daily, wrong for 1h/15m. Fix: parameterise via `bars_per_year` on `WalkForwardSplit` (252 for 1d, ~1638 for 1h US session, etc.).

### Enhancements (open)
- **Persist trade outcomes from IBKR** (`trade_log` table): prerequisite for realised-P&L Kelly, Phase 5 RL, and performance attribution. Subscribe to fill events on `IBKRConnection` and write entry/exit rows. **→ rolled into "Phase 4.5 — Realised P&L plumbing" below as Phase B.**
- **Position sizing in walk-forward**: wire `PositionSizer` into `MLWalkForwardOrchestrator` using forward-only Kelly history (from `trade_log`) to avoid lookahead bias. **→ rolled into "Phase 4.5 — Realised P&L plumbing" below as Phase C.**
- **Richer cost model**: bid-ask spread, partial fills, market impact in `models/walk_forward.py`.
- **Survivorship-bias column on `walk_forward_results`**: add `universe_policy` ∈ {`dynamic`, `static`} per row so dashboard Page 4 can flag biased runs. Today the warning is only logged.
- **Expand `_SECTOR_MAP`**: sector exposure check in `PortfolioGuard` currently silently passes unknown symbols.
- **Weekly log trim — framework-chatter demotions in the WF training loop** (deferred from 2026-04-30 audit, decision date: first Sunday after this lands, i.e. on/after 2026-05-03): the daily-loop trim (#1) only touched the fetcher / indicator / checkpoint-loader paths used by `run_daily.bat`. Weekly logs (`logs/weekly/*.log`, ~14k lines / ~1.6 MB on the 4/26 baseline) carry an additional ~2,800 lines of per-fold framework chatter that the daily trim doesn't catch. Pattern: each symbol runs 5 folds + 1 final retrain = 6 training passes, and each pass emits a 7-line stub-status block (`Ensemble training LSTM ...` / `LSTM training complete` / `Ensemble training XGBoost ...` / `XGBoost top features: {...}` / `Ensemble training FinBERT ...` / `FinBERT: using pre-trained weights (no fine-tuning)` / `Ensemble training complete. Weights: {<initial defaults>}`). None are decisions; the per-fold Sharpe/coverage/rebalance lines that follow already tell the diagnostic story.

  **Trigger condition**: hold until the **first Sunday weekly run after 2026-04-30** lands so we have a post-daily-trim baseline (some of the noise from `run_pipeline.py`'s Phase 2 fetch + `Persisted 0 indicator row(s)` + `Fetched and cached fundamentals` is already gone after the 4/30 commit). Compare the new line count against ~14k. If still > 12k, ship the trim; if it's already much closer to 10k from the daily-loop carry-over alone, reassess whether the marginal trim is worth the loss of "LSTM done" / "XGBoost done" breadcrumbs.

  **The trim** (when ready): demote to DEBUG —
    - `models/ensemble.py:59` `Ensemble training LSTM ...`
    - `models/ensemble.py:62` `Ensemble training XGBoost ...`
    - `models/ensemble.py:65` `Ensemble training FinBERT (pre-trained, no fine-tuning) ...`
    - `models/ensemble.py:68` `Ensemble training complete. Weights: %s` (these are the *initial* defaults loaded from config — same string every fold; the post-rebalance `Updated ensemble weights` line is what matters)
    - `models/lstm_model.py:186` `LSTM training complete (...)` (fold-level; the WF orchestrator's `Fold N complete - Sharpe=...` line a few seconds later confirms the entire fold)
    - `models/xgboost_model.py:114` `XGBoost top features: {...}` (prints 6×/symbol with slight rerankings; final-retrain importance is on Page 3 anyway)
    - `models/finbert_model.py:189` `FinBERT: using pre-trained weights (no fine-tuning)` (the message *itself* says it's a no-op; same string every call)

  **Keep at INFO** (these are decisions / phase markers / real events; do not touch):
    - `[<sym>] Starting 5-fold walk-forward | run_id=...`
    - `Fold N/5 -- train [...] | test [...]`
    - `Fold N: FinBERT coverage=X%`
    - `Fold N complete - Sharpe=..., return=...`
    - `Ensemble rebalance - best price model: <model> (Sharpe=...)`
    - `Updated ensemble weights: {...}` (post-rebalance — the weights that actually ship)
    - `Retraining ensemble on full dataset for live inference ...`
    - `LSTM saved to ...`, `XGBoost saved to ...`, `Ensemble models saved to ...` (1×/symbol, real artifact creation)

  **Tradeoff to weigh after the baseline**: post-trim, if LSTM trains successfully but XGBoost crashes mid-fold, you lose the "LSTM done" breadcrumb at INFO — only the XGBoost ERROR line surfaces, plus the absent `Fold N complete` (which never fires). Acceptable in exchange for ~20% size reduction, but the user should glance at the 5/3 weekly log first to make sure the framework chatter isn't catching anything subtle in practice.

- **Logging quality v2 — structured detail on risk decisions** (carry-over from 2026-04-30 audit, items #2 / #3 / #5): the noise-trim pass landed (HuggingFace silenced, per-symbol Phase 2 / Phase 3 logs demoted to DEBUG — see *Logging conventions* section above). Three substantive additions remain so the daily log lets you reconstruct any decision after the fact, without needing to grep DB tables.
    - **#2 — full `GuardResult.checks` on REJECT** (`risk/order_manager.py:178`, `risk/portfolio_guard.py`): today the rejection log line includes only `guard_result.reason` (the message of the *first* check that failed). The underlying `GuardResult.checks` dict — which records pass/fail for every check that ran — is dropped. Plumb the full breakdown into the log line (e.g. `[GLD] REJECTED — no_duplicate FAIL (Already holding) | passed: circuit_breaker, stop_sanity, portfolio_drawdown, position_size, sector_exposure, correlation`), and add a `failed_check` column to `order_decisions` so a future "why did 8 trades reject yesterday?" answer is one SQL filter, not 8 log greps.
    - **#3 — Kelly inputs at INFO** (`risk/position_sizer.py:93`): sizing math currently logs at DEBUG only. The APPROVED line in OrderManager shows the final share count but not how Kelly arrived at it. Promote the sizing log to INFO with structured fields: `[<sym>] sized N shares ($V, X.X% equity) | method=kelly | trades=42 p=0.55 b=1.42 f*=0.18 → f_used=0.045 (¼-Kelly, capped at 10%)`. When Kelly looks wrong (or the cold-start fallback engages), this is the breadcrumb. Pair this with Phase 4.5 Phase C (realised-Kelly) — once `compute_realised_kelly` lands, this same log line becomes the canonical "what did Kelly say today and why" record.
    - **#5 — Phase 5 reject-reason histogram + stale-bar rollup** (`scripts/signal_runner.py:_phase5_summary`, `_phase3_signals`): today Phase 5 only prints aggregate counts (`Orders rejected: 2`). Add (a) a one-line histogram grouped by guard check name (`Rejected: 8 no_duplicate, 3 sector_exposure, 1 stop_sanity`), driven off the new `failed_check` column from #2, and (b) a single rollup line at the *top* of Phase 3 instead of per-symbol skip lines mid-stream: `Universe: 68 symbols, 65 fresh, 3 stale (HAL 5d, ASTS 8d, EWJ 4d) — dropping stale`. Persist the histogram to a `reject_histogram` JSON column on `signal_runner_log` so Page 8 can chart reject-reason mix over time.
    - **Bonus — `run_id` on every log line.** Wrap the signal-runner logger in a `logging.LoggerAdapter` so every line emitted during a run carries `[<run_id_short>]`. Today `run_id` exists in DB tables but not in any log line, so `grep <uuid> trading_app.log` returns nothing — you have to read the file by timestamp. Adapter approach keeps modules unchanged (they still call `log.info("...")`); the adapter injects the contextual run_id into the format. Alternative: `extra={"run_id": run_id}` + a custom formatter — slightly more invasive.

  **Why deferred:** the noise-trim landed in the same session as the audit (mechanical: DEBUG demotions + env-var setting). These three add new structured fields and require touching `OrderManager`, `PositionSizer`, `PortfolioGuard.GuardResult`, the `order_decisions` and `signal_runner_log` schemas (via `_migrate()` ALTERs), Page 8 to render the new columns, and the Phase 5 summary. Worth a focused session once the noise-trim has been verified across a couple of daily runs and the user has a feel for what's still missing from the trimmed logs.

  **Verification baseline for the noise-trim**: post-2026-04-30, expected daily log size drops from ~1,700 lines to roughly **~600–800 lines** (Phase 2 and Phase 3 each shrink by ~75%). Weekly retraining logs (`logs/weekly/*.log`) were not touched — the WF training output dominates them and is largely useful, so it stays at INFO. If the next two daily runs come in materially above 800 lines, something else is leaking noise (most likely candidate: news-fetch loop in `run_pipeline.py`, which was not part of this pass).

- **Adopt IBC (IB Controller) for unattended IB Gateway operation**: IB Gateway silently logs out overnight (observed: user finds it logged out most mornings and must re-launch + re-enter credentials manually), which breaks `run_daily.bat` Phase 4 / Phase 3.5 whenever the morning task runs before the manual restart. [IBC](https://github.com/IbcAlpha/IBC) is the standard open-source wrapper: launches IB Gateway on boot, enters paper/live credentials from a config file, handles the daily 24h session reset, and auto-restarts on unexpected disconnect. Setup is a self-contained install (no code changes in this repo) — just point it at the existing `IB Gateway 10.x` install and wire it into Windows Task Scheduler in place of launching IB Gateway manually. Until this is in place, morning runs can silently fall back to dry-run (`⚠ IBKR unreachable — falling back to dry-run for this phase`) even though `paper_orders_enabled=True`. If IBC still proves flaky, the fallback plan is migration to Alpaca (pure REST API, no desktop app) — larger effort: rewrite `execution/ibkr_connection.py`, demote the IBKR news tier, rework `risk/trailing_stop.py` to Alpaca's `trail_price` / `trail_percent` order params, and replace the Page 9 IBKR account view. Alpaca supports bracket orders and native trailing stops so the risk-layer surface area stays similar.

- **Phase 4.5 — Realised P&L plumbing (brackets in WF + `trade_log` + realised-Kelly)** *(Phase A code complete 2026-04-29 — awaiting live verification; Phases B & C pending)*: Bundles four previously-separate items that share a single keystone — the `trade_log` table:
    - Bug: *Kelly disconnected from realised outcomes* (above)
    - Enhancement: *Persist trade outcomes from IBKR*
    - Enhancement: *Position sizing in walk-forward*
    - Enhancement: *Simulate brackets + trailing stops in WF*

  **Why bundle**: designing the schema once across both the WF simulator and the live fill subscription forces a clean definition of "what is a trade" (entry/exit/pnl, exit reason, partial-fill aggregation). Phase A populates `trade_log` with thousands of simulated trades on the first WF run, giving Phase C (realised-Kelly) training data on day one rather than waiting months for paper-trading fills to accumulate. Schema discipline now prevents a re-shape later when live fills land.

  **Why it matters now**: post-cost-fix, the SPY last-fold Sharpe came in at -11.50 (true close-to-close P&L on a 7-SELL window against a rallying market). With a 2× ATR stop the realised loss would have been bounded at ~-4 ATRs total instead of -12% close-to-close. A chunk of the negative-Sharpe signal across the universe is "no stops modeled," not bad alpha. Separately, Kelly is currently sized from `|ensemble_score|` as a P(win) proxy because there's no realised-outcomes data — solvable only once brackets in WF (or live fills) start populating a per-trade record.

  **Resolved design decisions** (locked in before code):
  | Question | Decision |
  |----------|----------|
  | Intra-bar order ambiguity (stop vs TP both in `[Low, High]`) | **Worst-case** — fill the stop. Standard backtesting bias; conservative. |
  | Gap-through (`Open <= stop` long, or `Open >= tp` long) | Fill at `Open`. The gap *is* the slippage; don't double-charge. |
  | TP slippage | **None.** Limit orders fill at limit or better; modeling slippage on TP is wrong. |
  | Stop slippage | `stop_slippage_multiplier × slippage_pct`, default `2.0`. New config field on `RiskConfig`. |
  | Re-entry after stop/TP/trailing fill | Position → 0; re-entry only on a fresh BUY/SELL gate signal. **No same-bar re-entry** (signal generated on bar `t` close enters on bar `t+1`). |
  | Trailing-stop ratchet basis | Use `High` for `peak_price` updates; **but new trailing-stop level only applies bar `t+1`+**. Today's intra-bar check always uses yesterday's end-of-bar stop level (avoids lookahead). |
  | Fold boundaries | Force-flatten at fold end regardless of bracket state. Bracket exits and fold-end flatten are independent — whichever fires first closes the position. |

  **Per-bar order of operations** (deterministic; document in `_run_test_window` docstring):
  1. **At bar open**: gap check. If long and `Open <= stop` → fill at `Open`. If long and `Open >= tp` → fill at `Open`. Symmetric for shorts.
  2. **Intra-bar (worst-case rule)**: if both `stop` and `tp` lie in `[Low, High]` on the same bar, fill the stop. If only one is touched, fill that one. Stop fills charged `stop_slippage_multiplier × slippage_pct`; TP fills exact.
  3. **At close** (only if still in position):
     - Pre-activation: check `Close >= entry + activation_atr × ATR`. If yes, replace fixed `(stop, tp)` with trailing; set `peak_price = High_t`.
     - Post-activation: ratchet `peak_price = max(peak_price, High_t)`; new trailing stop = `peak_price - trail_atr × ATR`. No TP cap on trailing positions.
  4. **Next bar** uses today's end-of-bar stop level for its intra-bar check.

  **Sequencing — three PRs, one connected effort:**

  **Phase A — `trade_log` schema + WF bracket simulation** (largest piece, contained to backtester math; ships standalone value):
  1. New `trade_log` table via `_migrate()` in `data/database.py`:
     ```
     id, source ('walk_forward' | 'live'), run_id, fold_index, symbol, signal,
     entry_ts, entry_px, exit_ts, exit_px,
     exit_reason ('stop' | 'tp' | 'trailing' | 'signal_flip' | 'fold_end' | 'manual_close'),
     shares, pnl, pnl_pct, costs_charged, recorded_at
     ```
  2. New config: `RiskConfig.stop_slippage_multiplier` (default 2.0); `RiskConfig.min_trades_for_realised_kelly` (default 30, used in Phase C).
  3. Extend `models/walk_forward.py:_run_test_window` to maintain per-position bracket state (`entry_price`, `stop_price`, `tp_price`, `trail_active`, `peak_price`, `trail_amount`, `entry_bar_ts`). Pull `atr_14` from the indicator dataframe; compute bracket levels at entry using `config.risk.atr_stop_multiplier` / `atr_take_profit_multiplier`.
  4. Implement the per-bar order of operations above.
  5. Persist each closed trade to `trade_log` with `source='walk_forward'`, `run_id=<wf_run_id>`, `fold_index=<i>`. Charge slippage + commissions per existing cost model, plus the extra stop-slippage on `exit_reason='stop'`.
  6. Optional: dashboard Page 4 "Exit reason" breakdown. Defer to follow-up; keep PR focused on backtester correctness.

  **Phase B — live fill subscription** (different risk profile: async + partial fills + order modifications):
  1. Subscribe to `execDetails` and `commissionReport` on `IBKRConnection`.
  2. Match fills to bracket order IDs via `parentId`; aggregate partial fills into a single trade row; resolve close → entry pairing.
  3. Write rows to same `trade_log` with `source='live'`.
  4. Best done after Phase A so the schema has been validated by real WF use first. Ship as a separate PR even if planned in the same sprint.

  **Phase C — realised-Kelly + WF position sizing** (small once A+B are in):
  1. New helper `risk/position_sizer.py:compute_realised_kelly(symbol, as_of, lookback_n)`: filters `trade_log` by `entry_ts < as_of` (forward-only by construction), returns `(win_rate, avg_win_pct, avg_loss_pct)` from the most recent `lookback_n` closed trades.
  2. `PositionSizer.calculate()` gains optional `kelly_history` argument; **cold-start fallback to `|ensemble_score|` proxy when fewer than `min_trades_for_realised_kelly` closed trades exist for the symbol.**
  3. Wire `PositionSizer` into `MLWalkForwardOrchestrator` reading `source='walk_forward'` rows from prior folds (forward-only — same `run_id`, lower `fold_index`).
  4. `signal_runner.py` reads `source='live'` rows for live Kelly sizing.

  **Testing** (extend `tests/test_walk_forward.py:TestCostModel` pattern with synthetic OHLC):
  - Stop intra-bar caps loss at `entry - atr_stop_multiplier × ATR × (1 + stop_slippage_multiplier × slippage_pct)`.
  - TP intra-bar locks gain at exactly `tp` (no slippage).
  - Gap-through stop fills at `Open` (not `stop`); no extra stop-slippage charge on top of the gap.
  - Both-touched bar fills the stop, not the TP (worst-case rule).
  - Trailing-stop activates only after `Close >= entry + activation_atr × ATR`.
  - Trailing-stop ratchet is monotonic (`peak_price` never decreases).
  - Trailing-stop level is "delayed by one bar" — today's High doesn't tighten today's stop.
  - Re-entry blocked on the same bar a stop fills; new entry happens on the next bar after a fresh signal.
  - Fold-end flatten triggers when no bracket exit has fired by the last bar.
  - `trade_log` rows written with correct `exit_reason` for each scenario.
  - Phase C: `compute_realised_kelly` only returns trades with `entry_ts < as_of` (forward-only invariant); cold-start fallback engages below threshold.

  **Order of attack**: do Phase A *after* the next weekly `--force` retrain runs cleanly under the bug-fixed cost model, so we have a baseline of "true close-to-close Sharpes" to compare against once brackets are layered in. That comparison is the actual answer to "is the underlying alpha bad, or is it the bracket-less WF that makes it look bad?"

  **Status (Phase A — 2026-04-29):** implemented.
  - **What was implemented:**
      * `data/database.py`: new `TradeLog` ORM (table `trade_log`) + `log_trade` / `log_trades_bulk` / `get_trade_log` helpers.  The reader supports a `before_ts` filter for Phase C's forward-only invariant.
      * `config/settings.py`: new `RiskConfig.stop_slippage_multiplier` (default 2.0) and `RiskConfig.min_trades_for_realised_kelly` (default 30, used in Phase C).
      * `models/walk_forward.py:_run_test_window`: rewritten as a per-bar event loop with explicit position state (`entry_px`, `stop_px`, `tp_px`, `trail_active`, `peak_px`, `trail_amount`, `entry_bar_ts`).  Per-bar order: pending entry → gap check → intra-bar worst-case → MTM → gate eval → trail update → signal-flip → fresh-signal scheduling → fold-end.  Entry timing changed: signals at bar t close enter at bar t+1 open (was: same-bar position change).  ATR for stop/TP at entry comes from the bar BEFORE entry (no lookahead); ATR for trail update comes from the current bar's close.  Returns a fourth tuple element `trades`; `MLWalkForwardOrchestrator.run` calls `log_trades_bulk` per fold.  Stop slippage applies only to intra-bar fills, not gap-throughs.
      * **Update 2026-04-30 — `allow_short_selling` gate (`models/walk_forward.py:_run_test_window`):** the simulator now reads `config.trading.allow_short_selling` and skips short-opens accordingly, mirroring `OrderManager.process` in live signal_runner.  Two patch points: (1) the post-signal-flip pending-entry assignment, (2) the fresh-signal pending-entry assignment.  Both gated on `result.signal == "BUY" or allow_short`.  Pre-fix WF audit (the trade_log dump that triggered this) showed 349 SELL trades out of 530 (66 %) — all of which would have been REJECTED_NO_POSITION live; isolating the BUY subset flipped Kelly f* from −0.084 to +0.062.  Without this gate the WF aggregate P&L was structurally pessimistic by an order of magnitude, leading to bad strategy-tuning decisions.
  - **Test coverage added:** 10 in `tests/test_walk_forward.py::TestBracketSimulation` — stop / TP / gap-through / worst-case tie / trail activation / ratchet / one-bar-delay / no same-bar re-entry / fold-end / trade-log schema.  4 existing `TestCostModel` tests updated for the entry-timing shift (4-tuple unpack + bar-shift assertions).  **+2 new (2026-04-30) for the long-only gate**: `test_long_only_sell_from_flat_is_noop` (SELL at flat opens nothing under default config) and `test_long_only_sell_after_long_closes_without_reopening_short` (BUY → SELL → no fold_end short).  The pre-existing `test_flip_buy_to_sell_counts_two_trades` is now wrapped in a `monkeypatch` setting `allow_short_selling=True` since it explicitly tests the shorts-allowed path.  Full suite: **146 passed**.
  - **Verification pending:** the next weekly `--force` retrain will be the first real-data run with the long-only gate.  Observable success signals: (1) `trade_log` row count drops sharply (~530 → ~180 expected, given 66 % were shorts); (2) every row's `signal` column is `BUY` (no `SELL` rows since `allow_short_selling=False` is the live default); (3) aggregate net P&L on the Trade History page should be much closer to break-even or positive than the prior −$662; (4) per-fold Sharpe distribution loses its long left tail (the worst folds were dominated by shorts on rallying ETFs).  If long-only WF still shows persistent Sharpe<0 across folds, that's the *real* signal that alpha needs work — distinct from the prior shorts-induced noise.  Once Phases B (live fills) and C (realised-Kelly) land, Phase 4.5 as a whole can be promoted to verified.

- **Trade History dashboard page + tax/net-profit analytics** *(code complete 2026-04-30 — awaiting first weekly retrain to populate `trade_log` with WF rows for live verification)* (new dashboard surface over `trade_log`): build a `dashboard/pages/10_Trade_History.py` page that turns the existing `trade_log` table into a human-readable record of closed trades, with realised P&L net of `costs_charged`, holding-period classification (short-term vs long-term), and an indicative tax-impact view. Phase 4.5 Phase A already populates the table with WF-simulated rows, so the page has data on day one; Phase B (live IBKR fills) will add `source='live'` rows to the same table later, and the page's `source` filter is what surfaces them.

  **Why it matters**: today there is no UI surface that answers "what trades have we actually taken, and what did we net after fees?" The Account page (`9_Account.py`) shows current positions and signal history; Page 4 shows WF Sharpe per fold; Page 8 shows order *decisions* (pre-fill). None of them show the *outcome* of closed trades. As soon as Phase B lands, this is the first place the user will look to answer "is the system profitable?" — the page should exist before that question is asked.

  **Why now (before Phase B)**: WF rows alone are useful for two things — (a) sanity-checking the bracket simulator's behaviour by eye (exit-reason breakdown, holding-period distribution, P&L distribution) and (b) building/iterating the page UI against real data instead of fixtures. When Phase B arrives, only a `source` filter toggle needs to flip to make it a live-trades page.

  **Page layout sketch**:
  - **Top filters (sidebar)**: source (`walk_forward` | `live` | `both`, default `live` once Phase B lands, `walk_forward` until then) · symbol multi-select · date range · exit_reason multi-select · `run_id` filter (free-text, useful for drilling into one WF run).
  - **Summary cards row** (5 cards): total closed trades · gross P&L · total `costs_charged` · **net P&L** (gross − costs) · win rate.
  - **Tax-impact section** (collapsible expander, defaults open):
      * Two cards: short-term realised gain (≤365-day holding period) and long-term realised gain (>365 days), each split into "gains" and "losses" so net positions are visible.
      * One card: estimated tax owed = `short_term_gain × short_term_rate + long_term_gain × long_term_rate` (only positive gains taxed; losses don't generate negative tax in this view — they offset gains, with carryforward shown as a separate line if net is negative).
      * Sidebar inputs: federal short-term rate (default 24% — single-filer middle bracket), federal long-term rate (default 15%), optional state rate (default 0%). Stored in `st.session_state`, not persisted to YAML — these are personal and shouldn't live in a shared config file.
      * Bold disclaimer at the top of the section: "Indicative only — not tax advice. IBKR's 1099 is the authoritative record. Wash-sale adjustments, lot-level cost basis, and broker-reported figures may differ from this view."
  - **Trades table** (main): one row per closed trade, columns = symbol · signal · entry_ts · exit_ts · holding_days · ST/LT badge · shares · entry_px · exit_px · gross_pnl · costs_charged · **net_pnl** · pnl_pct · exit_reason · source · run_id (truncated). Sortable; CSV export button. Color-code net_pnl (teal positive, red negative) per dashboard convention.
  - **Charts row** (2-up):
      * Cumulative net P&L over time (line, by `exit_ts`). One series per source if `both` is selected.
      * Exit-reason distribution (donut or horizontal bar) — quick read on whether stops, TPs, or signal flips are dominating.
  - **Per-symbol breakdown** (collapsible expander): table with symbol · n_trades · win_rate · avg_holding_days · gross_pnl · costs · net_pnl · ST_gain · LT_gain. Useful for "which names actually made money."

  **Computation conventions** (all derived on-the-fly in `data/ui_queries.py` — no schema changes needed):
  - `holding_days = (exit_ts - entry_ts).days`
  - `is_long_term = holding_days > 365` (calendar days; the IRS "more than one year" rule)
  - `net_pnl = pnl - costs_charged` (both columns already in `trade_log`)
  - `short_term_gain = sum(net_pnl for trades where holding_days <= 365 AND net_pnl > 0)`
  - `long_term_gain = sum(net_pnl for trades where holding_days > 365 AND net_pnl > 0)`
  - Symmetric `_loss` aggregates; net ST = gain − loss; net LT = gain − loss; net realised = net ST + net LT.
  - Tax estimate uses **net** ST and LT (after intra-class offset) — this is the approximation, not the full IRS netting rules (which let LT losses offset ST gains and vice versa with specific ordering). Footnote that limitation in the UI.

  **What's explicitly out of scope** (deferred, document inline on the page so future-me doesn't think they're missing):
  - **Wash-sale detection** (IRC §1091): requires scanning ±30 days around every loss for any buy in the same security and disallowing the loss, then adjusting the cost basis of the replacement lot. Real complexity; broker-reported numbers are authoritative anyway. Defer until there's a specific user need.
  - **Per-lot cost-basis methods** (FIFO/LIFO/specific-ID): `trade_log` is already entry/exit-paired, so lot accounting is implicit (one row = one lot). If partial-fill aggregation in Phase B ever splits a position across multiple entry fills against one exit, revisit.
  - **Multi-year tax reports** (1099-B reconciliation, Schedule D output): broker job, not ours.
  - **State tax nuance** (no-income-tax states, AMT, NIIT 3.8% surcharge above income threshold): keep to a single flat state rate input; users who need precision use TurboTax.

  **New `data/ui_queries.py` functions** (mirror existing `query_*` patterns, `@st.cache_data(ttl=300)`):
  - `query_trade_log(source=None, symbols=None, start=None, end=None, exit_reasons=None, run_id=None) -> pd.DataFrame`: thin wrapper over the existing `get_trade_log` helper in `data/database.py`, returning a DataFrame with `holding_days`, `is_long_term`, and `net_pnl` columns added.
  - `query_trade_summary(...same filters...) -> dict`: aggregates for the summary cards (n, gross, costs, net, win_rate).
  - `query_tax_breakdown(...same filters...) -> dict`: ST/LT gain/loss aggregates (no tax rates applied — rates stay in the page so they don't invalidate cache when the user fiddles).

  **Implementation order** (one PR, ~half-day):
  1. Add the three `query_*` helpers to `data/ui_queries.py`. Hand-test against current `db/trading.db` (which already has WF rows from the 2026-04-29 retrain).
  2. Build `dashboard/pages/10_Trade_History.py` per the layout above. Copy chart styling/conventions from Page 4 / Page 8.
  3. Update CLAUDE.md "Complete File Structure" and "Dashboard Conventions" sections to mention the new page (Page 10).
  4. No new unit tests — the page is pure read + presentation; logic is in the query helpers, which are simple enough to verify by eye against the DB.

  **Trigger to revisit / promote scope**: once Phase B lands and `source='live'` rows accumulate for 60+ days, the wash-sale question becomes real. Add it then if user asks; otherwise the indicative-only framing covers it.

  **Status (2026-04-30):** implemented.
  - **What was implemented:**
      * `data/ui_queries.py`: four new helpers — `query_trade_log`, `query_trade_summary`, `query_tax_breakdown`, `query_trade_log_filter_options`. The first three accept the same filter kwargs (`source`, `symbols`, `start_date`, `end_date`, `exit_reasons`, `run_id`) so cache keys stay aligned; symbols/exit_reasons are tuples for stable `@st.cache_data` hashing. Date filters are applied against `exit_ts` (the realisation date — what matters for tax-year bucketing). Derived columns: `holding_days`, `is_long_term` (>365 days), `net_pnl` (= `pnl − costs_charged`).
      * `dashboard/pages/10_Trade_History.py`: 5-section page per the spec — sidebar filters (source / symbols / exit-date range / exit reasons / run_id) + tax-rate inputs in `st.session_state`; summary cards row; collapsible tax-impact section with disclaimer + ST/LT cards + estimated tax owed; color-coded trades table (teal positive net P&L / red negative) with CSV export; cumulative-P&L line chart (overlays gross vs net so the fee gap is visible) + exit-reason donut; per-symbol breakdown expander.
      * Tax computation is intra-class only (ST loss offsets ST gain, LT loss offsets LT gain — no cross-class IRS netting). Effective rate per class = federal + state. Negative `total_net` shows a carryforward warning instead of negative tax. Defaults: 24% ST / 15% LT / 0% state.
  - **Test coverage added:** none — page logic is pure read+presentation; the four query helpers are simple enough to verify by inspection. Smoke-tested against the current empty `trade_log`: helpers return correctly-shaped empty results, page module compiles cleanly via `python -m py_compile`.
  - **Verification pending:** the first weekly `--force` retrain (Sunday 2026-05-03 `run_weekly.bat`) will populate `trade_log` with `source='walk_forward'` rows. Observable success signals: (1) page renders without errors when opened in browser; (2) summary cards show non-zero gross/net/fees/win-rate; (3) cumulative-P&L curve has both gross (dotted white) and net (teal) traces visibly diverging; (4) exit-reason donut shows a spread of stop/tp/trailing/signal_flip/fold_end (not just fold_end). Phase B (live IBKR fills) → adds `source='live'` rows; the existing source filter toggle surfaces them with no code change.

### Design notes (not bugs)

**FinBERT `evaluate()` is a stub**: Returns `{"total_return": 0.0, "sharpe_ratio": 0.0}` always. This is intentional (sentiment can't be evaluated like a price model) but it means FinBERT never wins the LSTM/XGBoost competition — hence the coverage-based weighting as a substitute quality signal.

**Walk-forward cost model is approximate**: `slippage_pct` and `commission_per_share` are applied as flat adjustments. No market impact, no partial fills, no bid-ask spread model. Sufficient for learning purposes.

**No position sizing in walk-forward**: The signal gate outputs BUY/SELL/HOLD but the walk-forward P&L assumes 1 unit per signal. The `risk/position_sizer.py` module (Phase 4) provides Kelly/ATR sizing for live trading via `signal_runner.py`, but is not wired into the walk-forward backtester — integrating it there would require forward-only sizing (no future data in Kelly history) to avoid lookahead bias.

**VIX cache behavior**: `RegimeDetector._get_vix()` serves the cached SQLite value when it is younger than 4 hours. When the cache is stale and the code is running inside a Streamlit session (`streamlit.runtime.exists()` returns `True`), the live yfinance fetch is **skipped** to avoid blocking the UI thread — the stale value is used with a log warning. The VIX cache is refreshed by running `python scripts/run_pipeline.py` (^VIX is always fetched at the start of the pipeline run). The Page 3 sidebar shows the current cached VIX value and its age.

**Streamlit file watcher suppressed**: `fileWatcherType = "none"` in `.streamlit/config.toml` suppresses torchvision import noise in logs. If the file watcher is needed for development, remove that setting.

## Testing Approach

Unit tests mock `ib_insync`, `yfinance`, and all database calls — no live connections or network needed. Run with `.venv/Scripts/pytest tests/ -v`.

- `test_data_pipeline.py`: patches `yfinance.Ticker` and `data.database` functions
- `test_ibkr_connection.py`: patches `ib_insync.IB`
- `test_walk_forward.py`: 23 tests covering WalkForwardSplit edge cases, compute_metrics, orchestrator integration
- `test_models.py`: 14 tests; patches `models.finbert_model.NewsClient` (not AlpacaClient — that module was deleted)
- `test_universe.py`: 15 tests; patches `alpaca.trading.client.TradingClient`, `data.fundamentals.FundamentalsClient`, `data.universe.get_bars`; uses `tmp_path` in-memory SQLite for DB roundtrip tests
- `test_risk.py`: 18 tests; uses in-memory SQLite (`mem_engine` monkeypatch fixture); patches `PortfolioGuard.check` and `OrderManager._get_latest_close` where needed; no IBKR or network required
- `test_signal_runner.py`: 6 tests; patches `signal_runner.OrderManager`; no live connections, yfinance, or DB needed

Integration tests (`verify_connection.py`) require a paper trading account open in IB Gateway or TWS.
`verify_universe.py` requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` env vars for Stages 1-3; DB helpers are tested without keys.
`verify_risk.py` requires no external services; uses the live `db/trading.db`.
