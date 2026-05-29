# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-driven algorithmic trading system connecting to Interactive Brokers (IBKR) via IB Gateway. Built as a learning platform with a Streamlit dashboard that explains each component visually. Python async/await throughout for IBKR; all other code is synchronous. Data pipeline uses yfinance → SQLite. Dashboard is Streamlit + Plotly.

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | IBKR connection (paper/live trading) | Complete |
| 2 | yfinance data pipeline + indicators + Streamlit dashboard | Complete |
| 3 | ML signal generation (LSTM, XGBoost, FinBERT ensemble) | Complete |
| 4 | Risk & portfolio management | Complete |
| 5 | RL optimizer (PPO, Sharpe reward) | Pending |
| 6 | Live trading transition | Pending |

## Feature Phasing

- This project uses phased rollouts (Phase A, B, C). When implementing a consumer/UI for data that depends on a producer phase, explicitly call out which phases must be complete for the feature to show real data, and surface this BEFORE writing code.

## Working With Me

### Reference Disambiguation
- When the user references numbered items (e.g., '#1 and #2'), confirm whether they mean items from CLAUDE.md, the current conversation, or a planning doc before acting.

## Setup

```bash
pip install -r requirements.txt
```

All commands must be run from the project root (so `config`, `core`, `data`, etc. resolve as packages). The project uses a `.venv` at `trading_app/.venv/`; activate it or prefix commands with `.venv/Scripts/python` on Windows.

Before using IBKR features, configure IB Gateway:
- **IB Gateway**: `Configure → Settings → API → Settings` — enable ActiveX and Socket Clients, socket port 4002 (paper) / 4001 (live), uncheck "Read-Only API"

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

# End-of-day bar refresh (overwrite mid-day partial bars with post-close values)
python scripts/refresh_recent_bars.py              # default: last 5 days, current universe + recently-acted + held
python scripts/refresh_recent_bars.py --days 10    # wider backfill window
python scripts/refresh_recent_bars.py --no-ibkr    # skip IBKR-positions union (use when Gateway down)

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

# Test IBKR news API (requires IB Gateway open)
python scripts/test_ibkr_news.py --symbol AAPL --days 30 --max 300

# Run all tests (no live IB Gateway or network needed)
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
├── run_daily.bat            — Mon–Fri scheduler: run_pipeline.py → universe_scheduler.py --rescore-now
│                              --no-signal-run → train_models.py (skip-existing) →
│                              backfill_benchmark_returns.py → signal_runner.py --no-dry-run;
│                              logs to logs/daily/daily_run_YYYYMMDD.log.  Backfill is positioned
│                              before the runner so Page 10's benchmark-relative view never lags the
│                              latest trade_log rows by a full day.
├── run_weekly.bat           — Sunday scheduler: universe_scheduler.py --run-now → run_pipeline.py →
│                              train_models.py --force → backfill_benchmark_returns.py;
│                              logs to logs/weekly/weekly_run_YYYYMMDD.log.  Universe refresh runs
│                              FIRST so pipeline + training operate on the freshly-rotated active set —
│                              brand-new symbols would otherwise hit training with no indicators / news /
│                              FinBERT scores and produce degraded checkpoints (reordered 2026-05-20 after
│                              the 2026-05-17 weekly review surfaced ~80–100 min of orphaned training on
│                              soon-to-be-deactivated symbols during a 48-symbol rotation).
├── run_eod.bat              — Mon–Fri post-close scheduler (16:30 ET): refresh_recent_bars.py;
│                              logs to logs/eod/eod_run_YYYYMMDD.log.  Wire via Windows Task Scheduler
│                              separately from run_daily.bat (which fires pre-market at 09:40 ET).
├── run_intraday.bat         — Mon–Fri intraday scheduler (12:00 ET + 15:30 ET): intraday_check.py
│                              --no-dry-run; logs to logs/intraday/intraday_run_YYYYMMDD_HHMM.log
│                              (HHMM in filename so multiple runs per day don't collide).  Two
│                              separate Task Scheduler entries (TradingApp\IntradayMidday +
│                              TradingApp\IntradayLateAfternoon) point at the same batch file.
│                              Ratchet-only by default; opt-in mid-day TP→TRAIL conversions via
│                              RiskConfig.intraday_trail_conversion_enabled.
│
├── scripts/                 — all user-invokable CLI entry points (importable as the `scripts` package).
│   │                          Run any of them from the project root with `python scripts/<name>.py`.
│   ├── __init__.py          — package marker + module docstring
│   ├── run_pipeline.py      — CLI: pre-caches ^VIX, fetches OHLCV + news + FinBERT scoring for all symbols
│   │                          reads active symbols from universe_assets when config.universe.enabled=True
│   │                          (run scripts/universe_scheduler.py --run-now first to populate universe_assets)
│   │                          --use-watchlist forces static watchlist even when universe.enabled=True
│   ├── universe_scheduler.py — Cron-style scheduler: Sunday full refresh + Mon-Fri Stage-3 re-score
│   │                           also runs signal_runner.py --dry-run after each rescore
│   ├── train_models.py      — Walk-forward training for all symbols; saves checkpoints to
│   │                          models/cache/{symbol}/; run between run_pipeline.py and signal_runner.py
│   │                          --symbol, --quick, --force, --interval flags
│   ├── signal_runner.py     — Daily automation: (Phase 1) reconcile off-cycle IBKR fills →
│   │                          refresh data → generate signals → trailing stops →
│   │                          hold-timeout flatten → risk/order decisions
│   │                          7-phase flow (1, 2, 3, 3.5, 3.6, 4, 5); Phase 1 calls
│   │                          execution/reconciliation.py before the CB/baseline check when
│   │                          not dry-run; --dry-run (default),
│   │                          --no-dry-run (submit live paper orders — requires IB Gateway +
│   │                          trading.paper_orders_enabled=True), --symbol, --schedule flags.
│   │                          Bracket orders submitted GTC with prices rounded to $0.01 tick size.
│   │                          Phase 3.6 is opt-in via config.risk.hold_timeout_enabled (default
│   │                          False) and flattens longs whose last passed-gate BUY in signal_log
│   │                          is older than config.risk.max_hold_days.
│   ├── intraday_check.py    — Intraday lightweight runner (Phase 1 CB check + Phase 3.5 trail
│   │                          re-evaluation against live IBKR price).  Scheduled at 12:00 ET and
│   │                          15:30 ET on weekdays via run_intraday.bat.  Does NOT regenerate
│   │                          signals, refresh data, fetch news, retrain models, rescore
│   │                          universe, or evaluate hold-timeouts (Phase 3.6) — those stay on
│   │                          the daily/weekly cadence.  Writes one row to intraday_run_log per
│   │                          invocation.  Gateway-down → status='gateway_down' row + exit 0
│   │                          (Task Scheduler retry-storm avoidance — see architectural-decision
│   │                          note "Intraday runner exits 0 on Gateway-down rather than raising").
│   │                          --dry-run (default), --no-dry-run (enables CB-flatten + opt-in
│   │                          trail-conversion paths), --symbol (informational only).
│   ├── backfill_benchmark_returns.py — Idempotent backfill of `trade_log.benchmark_return_pct`
│   │                            (raw SPY/benchmark return over each trade's holding period).
│   │                            Operates only on `WHERE benchmark_return_pct IS NULL` so re-runs
│   │                            are no-ops on already-populated rows.  Wired into `run_weekly.bat`
│   │                            after `train_models.py --force` (and `run_daily.bat` after the
│   │                            skip-existing training pass) so the column stays current across
│   │                            retrains.  See the "Benchmark-relative tracking" architectural-
│   │                            decision note for the raw-vs-net semantics.
│   ├── refresh_recent_bars.py — End-of-day refresh: overwrites the last `--days` (default 5) of
│   │                            OHLCV bars + indicator snapshots for the union of
│   │                            (active universe, recently-acted symbols via order_decisions
│   │                            last 14 days, currently-held IBKR positions).  Uses
│   │                            upsert_bars/upsert_indicators with overwrite=True to replace
│   │                            mid-day partial bars (written by morning signal_runner Phase 2)
│   │                            with final post-close values from yfinance.  Tolerates IB Gateway
│   │                            being down (--no-ibkr flag, also auto-degrades on connect failure).
│   │                            Invoked by run_eod.bat.
│   ├── reconcile_fills.py   — Phase B CLI: reconcile IBKR fills → fill_log + trade_log via
│   │                          execution/reconciliation.py (same core as signal_runner Phase 1);
│   │                          --since / --dry-run / --symbol.  Backfills off-cycle bracket fills
│   │                          within IBKR's retention window (shorter than the nominal 7d — see
│   │                          arch-decision note)
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
│   ├── ibkr_connection.py   — IBKRConnection async context manager; AccountSummary dataclass;
│   │                          paper + live port switching; all account/order methods are async;
│   │                          get_last_price(): 3-tier fallback (IBKR live → IBKR 15-min delayed → yfinance);
│   │                          get_executions(): reqExecutions wrapper (no server-side time filter — see
│   │                          arch-decision note); informational error codes {2104, 2106, 2107, 2119, 2158,
│   │                          300, 399, 10167, 10197, 10349, 202} are suppressed from WARNING logs
│   └── reconciliation.py    — Phase B live-fill reconciliation core: reconcile_fills() ingests IBKR
│                              executions → fill_log, aggregates paired round trips → trade_log
│                              (source='live'); shared by signal_runner Phase 1 + scripts/reconcile_fills.py
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
    ├── eod/                 — eod_run_YYYYMMDD.log (one per run_eod.bat execution at 16:30 ET)
    ├── intraday/            — intraday_run_YYYYMMDD_HHMM.log (one per run_intraday.bat execution;
    │                          HHMM in filename — two runs per day at 1200 and 1530 ET)
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

18 tables in `db/trading.db`. All timestamps are UTC-naive datetimes.

| Table | Key columns | Notes |
|-------|-------------|-------|
| `ohlcv_bars` | symbol, interval, timestamp, OHLCV | unique on (symbol, interval, timestamp); also stores ^VIX |
| `indicator_snapshots` | symbol, interval, timestamp, rsi_14, macd, bb_*, ema_*, atr_14, volume_sma_20 | recomputed from bars by IndicatorEngine |
| `fundamental_data` | symbol, fetched_at, pe_ratio, forward_pe, price_to_book, ev_to_ebitda, revenue_growth, earnings_growth, profit_margin, roe, debt_to_equity, current_ratio, free_cashflow, analyst_target | append-only history (no UNIQUE on symbol — multiple rows per symbol over time); 24h cache in `FundamentalsClient.get` prevents same-day duplicate inserts; readers use `get_fundamentals` (latest row by `fetched_at DESC`) or `get_fundamentals_history` (full series) |
| `news_cache` | symbol, article_id, published_at, headline, sentiment_score | upsert updates score only when stored score is None |
| `signal_log` | symbol, generated_at, bar_timestamp, lstm_score, xgb_score, finbert_score, ensemble_score, regime, signal, passed_gate, gate_reason | written by MLWalkForwardOrchestrator.predict() |
| `ensemble_weight_history` | lstm, xgb, finbert, trigger, recorded_at, symbol, run_id | written after each rebalance; `symbol`/`run_id` added 2026-05-14, pre-migration rows NULL |
| `walk_forward_results` | run_id, symbol, fold_index, train/test dates, sharpe_ratio, max_drawdown, win_rate, n_signals, sentiment_note, universe_policy | sentiment_note + universe_policy added via migration; universe_policy ∈ {`dynamic`, `static`, NULL (pre-2026-05-12)} |
| `universe_assets` | symbol PK, name, asset_class, exchange, is_fixture, stage, market_cap, avg_dollar_volume, stage3_score, active, added_at, last_scored_at, removed_at | dynamic universe candidates. `stage3_score` ∈ [0, 1] — rank-percentile blend of 20-day return + ADV (was `xgb_score` until 2026-05-10) |
| `universe_run_log` | run_id, run_type, stage, symbol_count, duration_seconds, recorded_at, notes | per-stage timing from universe selector |
| `circuit_breaker_log` | event, reason, daily_loss_pct, weekly_loss_pct, triggered_at, reset_at, recorded_at | TRIGGERED / RESET / AUTO_RESET events |
| `equity_snapshots` | snapshot_date (unique), net_liquidation, total_cash, unrealized_pnl, realized_pnl, recorded_at | per-day NLV baseline written once per `signal_runner.py` run (Phase 1) before any orders submit. Re-running the runner same day overwrites the row via `log_equity_snapshot`. The CB's daily/weekly loss-pct math reads these snapshots — without them the auto-trigger has no baseline to compare against |
| `order_decisions` | run_id, symbol, signal, decision, shares, entry/stop/tp prices, position_value, reject_reason, decided_at | per-signal decisions from OrderManager |
| `signal_runner_log` | run_id, run_date, mode, symbols_processed, signals_generated, orders_submitted, orders_rejected, skipped_duplicates, longs_closed, trailing_conversions, hold_timeouts, duration_seconds | daily run summaries |
| `trailing_stop_log` | run_id, symbol, action, shares, entry_price, current_price, atr, trail_amount, reason, decided_at | one row per position evaluated by TrailingStopManager per run (action ∈ CONVERTED / SKIPPED / FAILED) |
| `trade_log` | source ('walk_forward' \| 'live'), run_id, fold_index, symbol, signal, entry_ts, entry_px, exit_ts, exit_px, exit_reason, shares, pnl, pnl_pct, costs_charged, benchmark_return_pct, recorded_at, entry_exec_id, exit_exec_id, parent_order_id, account | closed-trade outcomes; populated by MLWalkForwardOrchestrator's bracket simulator (Phase 4.5 — Phase A) and by IBKR fill reconciliation (Phase B, shipped 2026-05-29 — `source='live'` rows). exit_reason ∈ stop / tp / trailing / signal_flip / fold_end / manual_close (`fold_end` is walk-forward-only; never written for live rows).  `benchmark_return_pct` (added 2026-05-19) is the raw price return on `config.data.benchmark_symbol` (default SPY) over the trade's holding period — NOT net of costs; deliberately asymmetric with `pnl_pct` to give the correct retail-alpha frame.  NULL for pre-migration rows + any row whose entry_ts / exit_ts has no benchmark bar; populated by `scripts/backfill_benchmark_returns.py`.  `entry_exec_id`/`exit_exec_id`/`parent_order_id`/`account` (added 2026-05-29, Phase B) link a `source='live'` row to the IBKR executions it was aggregated from; NULL on `walk_forward` rows; `exit_exec_id` is the per-round-trip dedup key (partial unique index `uq_trade_live_exit WHERE source='live'`) |
| `intraday_run_log` | run_id PK, run_timestamp, mode ('intraday'), status ('completed' \| 'gateway_down' \| 'cb_tripped' \| 'error'), daily_loss_pct, weekly_loss_pct, cb_tripped, positions_flattened, trailing_evaluated, trailing_ratcheted, trailing_converted, duration_seconds, error_message | one row per `scripts/intraday_check.py` invocation (12:00 ET + 15:30 ET on weekdays). Separate from `signal_runner_log` because cadence (multiple per day) and scope (Phase 1 CB + Phase 3.5 only — never writes to `signal_log` and never regenerates signals) differ. `status='gateway_down'` rows have NULL loss_pct fields and are written even when IBKR is unreachable so missed slots are visible on Page 8.  `trailing_ratcheted` counts positions where IBKR has moved `Order.trailStopPrice` up since the last `trailing_stop_log` entry for that symbol (intraday-runner observability — see new `RATCHETED` action value). |
| `fill_log` | exec_id (UNIQUE), order_id, perm_id, parent_order_id, account, symbol, conid, side ('BUY'\|'SELL'), order_type, shares, price, commission, realized_pnl, exec_time, recorded_at | raw IBKR executions ingested by Phase B reconciliation (shipped 2026-05-29) — the audit trail from which `trade_log` `source='live'` rows are aggregated. `exec_id` is the sole idempotency key; only `commission`/`realized_pnl` are ever mutated after insert (commissionReport can arrive on a later fetch than the Execution — `upsert_fill` refreshes them when previously NULL). Written by `execution/reconciliation.py`; populated via `IBKRConnection.get_executions()`. |
| `reconciliation_state` | source, account, last_reconciled_ts, last_run_ts, last_n_fills, notes; UNIQUE(source, account) | Phase B reconciliation watermark (one row per source/account). `last_reconciled_ts` = newest exec_time persisted so far; seeds the next run's window display (NULL first run → now − 7d). Note: IBKR's effective retention is shorter than the 7-day nominal (see arch-decision note) so the watermark is advisory — ingestion takes IBKR's full returned set regardless. |

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
| `DataConfig.benchmark_symbol` | `"SPY"` | Benchmark used by Page 10 relative-performance section.  Fetched unconditionally by `run_pipeline.py` and `refresh_recent_bars.py` (same convention as `^VIX`) so ingestion does not depend on universe mode |
| `MLConfig.signal_threshold` | 0.35 | Base gate threshold |
| `MLConfig.signal_lookback_days` | 365 | Default date range on Model Signals page |
| `MLConfig.signal_confirmation` | 2 | Models that must agree (of 3) |
| `MLConfig.ensemble_*_weight` | 0.40/0.35/0.25 | Initial weights (normalised to 1.0 on save) |
| `MLConfig.ensemble_weight_floor` | 0.10 | Minimum per-model weight after rebalance |
| `MLConfig.ensemble_nudge` | 0.10 | Max weight transfer per rebalance |
| `MLConfig.news_available_from` | None | Walk-forward folds before this date suppress FinBERT. When `None` (default), the cutoff is derived per-symbol from `MIN(news_cache.published_at)` — set a date only to override (e.g. when a symbol's earliest cached news is unreliable) |
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
| `RiskConfig.intraday_trail_conversion_enabled` | False | When True + paper_orders_enabled, allow `scripts/intraday_check.py` to perform new TP→TRAIL conversions at 12:00 / 15:30 ET slots.  Default off — ratchet-only intraday mode protects against the no-stop window risk during mid-day cancel+place sequences |
| `RiskConfig.intraday_conversion_buffer_atr` | 0.5 | Additional buffer above `trailing_stop_activation_atr` required for intraday conversions: `current >= entry + (activation_atr + intraday_conversion_buffer_atr) × ATR`.  Keeps mid-day conversions to genuinely-strong moves only |
| `RiskConfig.hold_timeout_enabled` | False | When True + paper_orders_enabled, flatten held longs whose most recent passed-gate BUY in `signal_log` is older than `max_hold_days` |
| `RiskConfig.max_hold_days` | 30 | Calendar-day threshold for the hold-timeout rule (Phase 3.6). 0 disables defensively |

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
- IBKR news goes back ~4-5 months (300 article hard cap). By default `MLConfig.news_available_from` is `None` and `MLWalkForwardOrchestrator._resolve_news_cutoff()` derives the effective cutoff per-symbol from `MIN(news_cache.published_at)` — so the gate auto-adjusts to whatever's actually in the cache without a hardcoded date to maintain. Set `MLConfig.news_available_from` only to override (e.g. a sparse-news symbol whose earliest article isn't trustworthy). When neither source is set (empty cache, no override), coverage scaling is the safety net — bars with no news score 0, so FinBERT's weight falls toward 0 via the per-fold `finbert_coverage` multiplier.
- `upsert_news` updates `sentiment_score` only when the stored value is `None` — it never overwrites a previously scored article.
- FinBERT coverage (fraction of test-window bars with non-zero score) is tracked per fold and stored in `sentiment_note`. Coverage < 100% scales FinBERT's weight proportionally.

## Dashboard Conventions

All 6 pages follow the same patterns:

- **Chart style**: `template="plotly_dark"`, teal `#26a69a` for bullish/positive, red `#ef5350` for bearish/negative, `margin=dict(l=0, r=0, t=40, b=0)`
- **Data access**: all pages query SQLite via `data/ui_queries.py` functions decorated with `@st.cache_data(ttl=300)`. Pages never hit yfinance or the network directly (except Page 2's "Fetch & Score News" button and Page 6's "Refresh from IBKR").
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
- **Page 9** (`9_Account.py`): live IBKR account summary + positions (enriched with risk levels + live yfinance prices + cross-referenced active orders so cancelled bracket legs don't render as live) + open orders; position allocation donut. Positions converted to trailing stops surface a "Trailing Stop" card (trail distance + IBKR's live ratcheting trigger) in place of the original Stop / TP cards. IBKR required — no signal/trade history (Page 3 covers signals, Page 8 covers order decisions, Page 10 covers closed trades)
- **Page 10** (`10_Trade_History.py`): closed trades from `trade_log`; 5 summary cards (gross/net P&L, fees, win rate); **Benchmark-Relative Performance section (added 2026-05-19)** — 3 cards (cumulative excess return, win rate vs benchmark, avg excess per trade) + cumulative-excess chart + per-exit-reason excess table + per-trade audit expander; headline metrics exclude `exit_reason='fold_end'` (backtest artifacts) with an opt-in audit toggle in the expander; indicative tax view (ST vs LT split, configurable rates in `st.session_state`, *not* tax advice); color-coded trades table with CSV export; cumulative net-P&L curve (gross vs net) + exit-reason donut; per-symbol breakdown expander

## Key Architectural Decisions

**SQLite over Postgres**: Simplicity — no server to run, single-file database, sufficient for a single-user learning tool. All timestamps stored UTC-naive to avoid SQLAlchemy timezone complexities.

**`_migrate()` pattern over Alembic**: Alembic is heavyweight for a project at this stage. `_migrate()` with idempotent `ALTER TABLE` checks runs automatically at engine init and handles the only common DDL operation (adding columns). Adding a new column = add one `if "col" not in cols:` block.

**`as_of` threading through Ensemble → FinBERT**: The walk-forward orchestrator passes each bar's timestamp as `as_of` so FinBERT only uses news published on or before that date. Without this, FinBERT would aggregate today's news for every historical bar — a severe lookahead bias. The `as_of` parameter also skips the live API fetch for historical bars (no point fetching; cached articles are filtered in-memory instead).

**Coverage-based FinBERT weighting**: FinBERT's `evaluate()` can't produce a Sharpe ratio (sentiment can't be backtested like a price model), so it's excluded from the LSTM/XGBoost Sharpe competition. Instead, its weight scales with `finbert_coverage = non-zero bars / total bars` in each test window, reset from the configured baseline each fold (no drift). This ensures sparse-news folds automatically reduce FinBERT's influence.

**asyncio event loop before ib_insync import**: `ib_insync` depends on `eventkit`, which calls `asyncio.get_event_loop()` at import time. On Python 3.10+ in non-main threads (Streamlit's ScriptRunner), this raises. The fix is to create and set a new event loop *before* the `from ib_insync import ...` statement in `NewsClient._fetch_from_ibkr_standalone()` and `9_Account.py`.

**LSTM checkpoint format (torch tensors)**: PyTorch ≥ 2.6 changed `weights_only` default to `True`. Storing `pd.Series` in checkpoints breaks this (unsafe type). Fix: store `mean`/`std` as `torch.tensor(series.values)` and feature names as a plain `list`. Load converts tensors back to Series. Old checkpoints fall back to `weights_only=False` with a warning — retrain to upgrade.

**Three-tier news fallback (IBKR → Alpaca → yfinance)**: IBKR provides the best-quality financial news (~4-5 months back, 300 articles max) but requires IB Gateway. Alpaca is the secondary source (requires free API key). yfinance is the always-available fallback. The tiering means the system degrades gracefully without any API keys configured.

**`upsert_news` never overwrites scores**: Scoring is expensive (FinBERT ~400MB model). Once an article has a score it's permanent. The upsert only fills in `sentiment_score` when it's currently `None`. This lets `run_pipeline.py` be re-run idempotently — only new unscored articles are processed.

**`data/walk_forward.py` vs `models/walk_forward.py`**: Two layers intentionally. `data/walk_forward.py` is model-agnostic and usable with any strategy function (useful for Phase 4/5 development). `models/walk_forward.py` is the ML-specific orchestrator that wires the ensemble, signal gate, cost model, and DB persistence together.

**`score_series()` on LSTMModel**: Added to support the LSTM Analysis charts (Page 3). Runs inference bar-by-bar over a full DataFrame — first `seq_len` bars return NaN (no complete window). Returns a Series with the same DatetimeIndex as `df` for direct chart overlay.

**Universe survivorship bias**: When `MLWalkForwardOrchestrator` is constructed with `universe_selector`, it logs a warning that walk-forward results may be biased — the universe was determined using today's data, so historical folds can include symbols that were only selected in hindsight. For unbiased backtests, use the static watchlist (`--use-watchlist` flag or leave `config.universe.enabled=False`).

**Stage 3 pre-scoring OHLCV backfill**: `_stage3_score` in `data/universe.py` fetches bars via `DataFetcher.fetch_symbol(days_back=365)` for any Stage 2 survivor missing OHLCV before running the ranker. Without the backfill, `get_bars` returns an empty DataFrame → momentum input is missing → the symbol is ranked last on the momentum axis → the universe calcifies around previously-tracked symbols (observed: 2026-04-19 weekly run had 249/300 candidates zero-scored under the old XGBoost ranker). Cost: ~5-10 min of yfinance calls per full refresh, added to the existing Stage 2 duration (~70 min). Scored / no-data counts are logged for visibility.

**Stage 3 ranker is momentum + ADV rank-percentile, not a model** (`data/universe.py:_stage3_score`): `score = 0.5 × pct_rank(20-day return) + 0.5 × pct_rank(avg_dollar_volume)`. Both axes use rank-percentile so the score is robust to outliers (a $10B/day mega-cap doesn't dominate the score scale) and invariant to monotonic transformations of either input. Symbols with fewer than 21 cached bars get the worst-rank momentum input. The previous Stage 3 ranker loaded **the first XGBoost checkpoint it found alphabetically** (`models/cache/AAOI/xgb.ubj`, found 2026-05-10) and applied that single per-symbol classifier to every other Stage 2 survivor — meaningless as a cross-sectional ranking signal, since the model had only ever seen AAOI's price/indicator history. Symptom: 2026-05-10 weekly refresh dropped INTC (WF Sharpe = 2.137, the joint top of the universe), BA (1.488), AZN (1.145), VALE (1.788), and UAL (2.255) because the AAOI model rated them low — INTC scored −0.4873 against the active pool's 0.88–0.99 cluster. The new ranker has no ML dependency; `scripts/universe_scheduler.py` no longer loads any checkpoint, and `UniverseSelector.__init__` no longer accepts `xgb_model`. Migration `2026-05-10` renamed `universe_assets.xgb_score` → `stage3_score` in place (preserved existing values).

**Risk module dry-run default**: `signal_runner.py` defaults to `dry_run=True` (argparse `default=True`). To actually submit orders, pass `--no-dry-run` AND set `paper_orders_enabled=True` in config (for SIMULATION mode) or switch to LIVE mode. This two-gate approach prevents accidental order submission.

**Within-session deduplication (`EQUIVALENT_PAIRS`)**: `signal_runner.py` defines `EQUIVALENT_PAIRS = {"GOOG": "GOOGL", "GOOGL": "GOOG"}`. Phase 4 tracks `decided_symbols` across the run; if a symbol's equivalent has already been decided, the second symbol is skipped (no `OrderManager.process()` call, no DB record written). The `skipped_duplicates` count is logged in Phase 5 and persisted to `signal_runner_log`. `PortfolioGuard`'s built-in GOOG/GOOGL check only fires against live IBKR positions; this within-session guard is needed because `positions={}` in dry-run mode means the guard sees no existing positions to compare against.

**Long-only SELL handling (`allow_short_selling=False`)**: `OrderManager.process()` intercepts SELL signals before position sizing and the portfolio guard when `config.trading.allow_short_selling=False` (the default). If an existing long is found in `positions` → `_close_long_position()` is called (market sell to flatten the position) and `decision='CLOSED_LONG'` is returned. If no long is held → `decision='REJECTED_NO_POSITION'` is returned with no order placed. The guard and position sizer are bypassed entirely for close orders (closing reduces risk; no new sizing is needed). `longs_closed` is tracked in Phase 4 and persisted in `signal_runner_log`. Page 8 shows CLOSED_LONG in purple and REJECTED_NO_POSITION in amber. When `allow_short_selling=True`, the normal bracket-order path runs for SELL signals (future use — currently unreachable in practice). The walk-forward bracket simulator (`models/walk_forward.py:_run_test_window`) reads the same flag and applies the same rule: a SELL from flat is a no-op, a SELL after a long is a close-only signal_flip with no short scheduled. Without this gate, WF aggregate P&L was systematically misleading — 2026-04-30 audit found 66% of simulated trades were short opens that the live runner would never have executed; the long-only subset had +0.06 Kelly while the combined aggregate had −0.08 Kelly.

**Stop-price sanity check in PortfolioGuard**: Check #2 of the 7-check sequential guard (between `circuit_breaker` and `portfolio_drawdown`) verifies the stop price sits on the loss side of the entry price for the given signal — BUY requires `stop < entry`, SELL requires `stop > entry`. Guards against a bad ATR (NaN → 0 → `stop == entry`) or a sign-flip in stop placement, either of which would turn the safety stop into an instant or inverse-direction trigger. Also rejects `entry <= 0` and `stop <= 0`.

**`REJECTED_TOO_SMALL` short-circuits the order flow**: `OrderManager.process()` checks `pos_size.shares < 1` immediately after `PositionSizer.calculate()` and returns `decision='REJECTED_TOO_SMALL'` without calling the PortfolioGuard or submitting any IBKR order. This covers two scenarios: (a) Kelly/fixed sizing produced `position_value < entry_price` (tiny allocation on a high-priced stock); (b) `_get_latest_close()` returned 0 because no bars were cached for the symbol. Without this check, a 0-share "APPROVED" decision would be written to `order_decisions` and (in live mode) IBKR would either reject or silently no-op a 0-share bracket order.

**Circuit breaker is shared state**: `CircuitBreaker` reads/writes the `circuit_breaker_log` table in the shared SQLite DB. The dashboard (Page 8), `signal_runner.py`, and `universe_scheduler.py` all share the same state. The short `ttl=30` on `query_circuit_breaker_status()` means the dashboard reflects reality within 30 seconds without manual refresh.

**PortfolioGuard sector check is best-effort**: Sector exposure blocking only works for symbols in the hardcoded `_SECTOR_MAP` dict in `risk/portfolio_guard.py`. Unknown symbols (most mid/small-caps) pass through without a sector check. To extend coverage, add entries to `_SECTOR_MAP`.

**Universe Stage 1 requires Alpaca API keys**: `UniverseSelector._stage1_fetch()` calls `TradingClient.get_all_assets()`. Without keys it raises `UniverseError`. Permanent fixtures are always added regardless, so `run_full()` with no keys will produce a fixture-only list rather than crashing.

**Batch files over persistent scheduler**: Production automation uses `run_daily.bat` (Mon–Fri 09:40) and `run_weekly.bat` (Sunday 01:00) driven by Windows Task Scheduler, not a persistent `universe_scheduler.py --forever` process. Each batch file runs steps sequentially and exits — this avoids silent failures from processes dying overnight, multiple instances on re-login, and race conditions between pipeline and training. Daily training skips existing checkpoints (no-op after first run); weekly uses `--force` for full retraining. `set PYTHONUTF8=1` is set in both batch files to handle Unicode in log output on Windows.

**`get_last_price()` 3-tier market data fallback**: `IBKRConnection.get_last_price()` tries: (1) `reqMarketDataType(1)` live snapshot — requires real-time API subscription in IBKR Client Portal; (2) `reqMarketDataType(3)` 15-minute delayed data — free, no subscription needed, uses `snapshot=False` (required for delayed); (3) yfinance `fast_info.last_price` — always available. Error 10089 (no real-time subscription) is expected without a subscription and triggers automatic fallback to delayed data.

**Phase-4 live-order wiring (`--no-dry-run`)**: `signal_runner._phase4_risk_orders` opens a single event loop and a single `IBKRConnection` at the top of the phase, reuses both for every `OrderManager.process()` call, and closes them in a `finally` block. The loop is set as current via `asyncio.set_event_loop(loop)` **before** `IBKRConnection()` is instantiated — `ib_insync` calls `asyncio.get_event_loop()` during `IB()` construction and raises on Python 3.13 if no loop is bound to the thread. `OrderManager` now accepts an `event_loop` parameter so `_submit_bracket_order` / `_submit_market_close` reuse the same loop instead of creating a fresh one per call. If IBKR is unreachable mid-phase, the runner prints `⚠ IBKR unreachable — falling back to dry-run for this phase` and continues with `dry_run=True`.

**Bracket orders use GTC + tick-rounded prices**: `IBKRConnection.place_bracket_order` applies two fixes that keep brackets alive end-to-end: (1) `round(price, 2)` on entry / stop / TP to satisfy IBKR's minimum-tick-variation check (error 110 — `ib_insync` passes prices through float32 on the wire, which drifts e.g. 202.52 → 202.52000427246094); (2) `leg.tif = "GTC"` on every leg so the bracket survives if the runner fires outside RTH (DAY-TIF orders are immediately cancelled after the 16:00 ET close, which is why error 10349 "Order TIF was set to DAY based on order preset" lost the LMT legs before GTC was added). The $0.01 tick size is correct for all US equities on the current watchlist; sub-penny stocks would need a per-contract tick lookup via `reqContractDetails`.

**STP trigger price lives on `auxPrice`, not `lmtPrice`**: IBKR stores stop-trigger prices in the `auxPrice` field on STP / STP LMT orders; `lmtPrice` is only populated for LMT / STP LMT legs. `OrderResult` has both `limit_price` and `stop_price` fields, and `__str__` renders stops as `@ stop $191.92`. `IBKRConnection.get_open_orders()` returns both. `ib_insync` fills unused price fields with `sys.float_info.max` (~1.8e308) rather than `None` — the `_clean_price()` helper in both `place_bracket_order` and `get_open_orders` treats any non-finite value, anything `> 1e100`, or exact zero as "no price" and returns `None`. Without that sanitisation the Account page renders `$nan` for STP rows.

**Informational error codes continue to expand**: The `informational` set in `IBKRConnection._on_error` now includes `202` (Order Canceled confirmation — logged at ERROR by ib_insync but it's just the ack for a successful `cancelOrder`), `10349` ("Order TIF was set to DAY based on order preset" — IBKR's preset may rewrite TIF even when the client sets GTC explicitly), `10148` ("Order cannot be cancelled — already in Cancelled state" — benign race during cancel+place flows like trailing-stop conversion or long-only close, where the bracket child was already cancelled by IBKR before our cancel arrived; the subsequent place-order call still succeeds), and `10089` ("Requested market data requires additional subscription — Delayed market data is available" — same conceptual fallback as 10167 in a different IBKR wire format; the 3-tier `get_last_price` fallback handles it.  Added 2026-05-21 after the intraday runner produced 13 of these per scheduled slot — one per evaluated symbol — which were correctly handled but flooded the daily log at ERROR level). Full current set: `{2104, 2106, 2107, 2119, 2158, 300, 399, 10148, 10167, 10197, 10349, 202, 10089}`.

**`trade_log.pnl` is already net of costs — `costs_charged` is exposed separately for display only** (`models/walk_forward.py:_close_trade`, `data/ui_queries.py:query_trade_log`). The bracket simulator computes `pnl_pct = gross_pct - total_costs` and writes `pnl = pnl_pct × entry_px × shares`, so the stored `pnl` is the realised *net* dollar P&L. `costs_charged` carries the dollar cost component for display reconstruction. The non-obvious consequence: **never compute `net_pnl = pnl - costs_charged`** — that double-counts fees. The 2026-05-07 SPY verification caught this: a real net −$966.79 trade displayed as −$1,127.10 on Page 10 because `query_trade_log` was subtracting costs that were already deducted upstream. The corrected derivation is `net_pnl = pnl` and `gross_pnl = pnl + costs_charged` (back-derived), now pinned by 6 regression tests in `tests/test_ui_queries.py` (`test_net_pnl_equals_stored_pnl` is the canary — it explicitly fails on the buggy formula). Phase A picked this storage convention so `pnl_pct` stays a valid input for realised-Kelly (Phase C). Phase B's live IBKR fill subscription must follow the same rule when populating `source='live'` rows: write `pnl` as net of commissions/slippage so realised-Kelly across both sources is computed consistently.

**Page 10 dedup sources truth from `walk_forward_results`, not `trade_log`** (`data/ui_queries.py:_keep_latest_run_per_symbol`): each weekly `--force` retrain bulk-inserts a fresh batch of WF trades with new `run_id`s without truncating prior rows, so without dedup the page stacks every historical retrain on top of itself (4× duplicates after 4 weekly runs). The non-obvious choice is *what to dedup against*. Sourcing "latest run_id per symbol" from `trade_log` itself looks tempting but is **wrong**: a fresh training run that produces zero closed trades for a symbol (e.g. long-only gate suppressing every short, no buys firing in the test window) writes no rows to `trade_log`, so the dedup silently falls back to the *previous* run and surfaces stale pre-fix history that the current model no longer produces. The 2026-05-04 verification of the long-only gate caught exactly this: 39 SELL rows from 5 symbols (BA / CHTR / CRCL / IWM / NFLX) survived initial dedup because their 2026-05-03 retrain produced zero closed trades. `walk_forward_results` writes one row per `(run_id, symbol, fold_index)` on **every** fold regardless of trade count, so it always reflects the current training session. Symbols whose latest WF run produced zero trades correctly disappear from the deduped view — that's the right semantic for "no trades in current model" (vs. "stale rows from a previous model"). Live (`source='live'`) rows pass through dedup untouched — every fill is a unique trade. Specific `run_id` filter on the page short-circuits the dedup automatically.

**Trailing stops run as Phase 3.5, after signal generation and before new-order submission** (`scripts/signal_runner.py`, `risk/trailing_stop.py`). `TrailingStopManager.manage()` walks every long IBKR position, finds its bracket's LMT take-profit and STP stop-loss legs in `get_open_orders()`, fetches `atr_14` from `indicator_snapshots` and the latest daily close from `ohlcv_bars`, and when `current_price ≥ entry + activation_atr × ATR` performs Cancel-TP → Cancel-STP → Submit-TRAIL. The conversion order is intentional: the alternative (submit TRAIL first, cancel bracket legs after) would create a multi-second window in which STP and TRAIL are both live in different OCA groups, so a severe gap-down could trigger both and leave the account short by the position size. The current order leaves a sub-second "no stop" window instead — acceptable in normal markets. Idempotent: positions with an existing TRAIL order are skipped. Phase 3.5 is skipped entirely in dry-run mode, when `paper_orders_enabled=False`, or when `trailing_stop_enabled=False` (the default — opt-in feature). Standalone TRAIL orders use `orderType="TRAIL"` with `auxPrice=trail_amount` (rounded to $0.01 to satisfy IBKR minimum-tick-variation, same as bracket prices) and `tif="GTC"`. `OrderResult.__str__` renders TRAIL as `@ trail $X` using the `stop_price` field (which carries `auxPrice` — the trailing distance, not a trigger level). Short positions are skipped (long-only codepath); once `allow_short_selling=True` is wired, a symmetric BUY trailing stop path would be needed.

**Hold-timeout runs as Phase 3.6, after trailing stops and before new-order submission** (`scripts/signal_runner.py:_phase3_6_hold_timeouts`). For each held long, queries `signal_log` for the most recent passed-gate BUY via `data.database.get_latest_buy_signal_ts(symbol)`; if the latest BUY is older than `config.risk.max_hold_days` calendar days, the position is flattened with a market sell after cancelling bracket children (LMT TP, STP, STP LMT, TRAIL — same 4-type filter as `OrderManager._cancel_bracket_children`, since by Phase 3.6 some positions have already been converted in Phase 3.5). The phase order matters: running 3.6 *before* 3.5 would cancel TRAIL conversions the same morning they were created if the underlying BUY history is already stale; running 3.6 *after* Phase 4 (new orders) would let a stale-BUY position emit *another* bracket on a fresh BUY signal in the same run before flattening — harmless but noisy. Placement between 3.5 and 4 means "let winners trail, let stale ones go, then take new entries." Symbols with NO BUY in `signal_log` are skipped explicitly (manual positions / pre-history holdings have no anchor for staleness — flattening them would surprise the user). Phase 3.6 is skipped entirely in dry-run, when `paper_orders_enabled=False`, when `hold_timeout_enabled=False` (the default — opt-in feature), or when `max_hold_days <= 0` (defensive — would otherwise flatten every held long). Each closure is persisted to `order_decisions` with `decision='CLOSED_TIMEOUT'`; per-run count surfaces in `signal_runner_log.hold_timeouts` and Phase 5 stdout. The "re-confirming signal" semantic was chosen over a pure time-based stop (the alternative in the original TODO) because it preserves winners that the model still actively likes — a position fresh BUY-confirmed yesterday is not stale, only one ignored by the model for a full month is.

**Fold-end closures are backtest artifacts, not strategy decisions** (`models/walk_forward.py:_run_test_window`, surfaced by `dashboard/pages/10_Trade_History.py` benchmark section): `exit_reason='fold_end'` rows are positions force-flattened at the last bar of a WF test window because no stop / TP / trailing / signal_flip had fired by that bar.  They are mechanically correct (the bracket simulator must close every position before the next fold begins, otherwise positions would leak across fold boundaries and contaminate the next fold's training data with implicit lookahead) but they are **not exit decisions the live system would ever make** — live trading has no fold boundaries.  Two consequences that compound:
- **Left-truncation bias toward winners-still-running**: in any rising market, fold_end exits are disproportionately positions whose ATR stops did NOT fire (= they were winning at test_end).  The stops fired on losers earlier in the same fold.  Per-exit-reason aggregates make this explicit: 2026-05-19 baseline showed `fold_end` averaging **+1.27 % excess** vs the benchmark while the strategy-decided subset (stop/tp/signal_flip/trailing) averaged **−2.04 % excess** — opposite signs, same trades, same window.
- **fold_end is ~half of `trade_log`**: 763 / 1,602 rows (47.6 %) on the 2026-05-19 baseline.  Excluding fold_end is not a fringe filter — it removes nearly half the data.  Sample size remains ample (839 strategy-decided rows on 2026-05-19, 932 on 2026-05-24), and the negative-excess picture becomes visible only after fold_end is removed.

**Dedup vs raw views are honest answers to different questions** (`data/ui_queries.py:_keep_latest_run_per_symbol`, surfaced by Page 10's `dedup_to_latest_run` sidebar checkbox).  Same `trade_log` rows, two legitimate aggregations:
- **Deduped** (default ON, current model only): keeps the latest WF run_id per symbol.  Answers "what does the current model do?"  2026-05-24 baseline (strategy-decided, fold_end excluded): **n=93, cumulative excess = +538.11 %, win-rate-vs-benchmark = 48.4 %** (2026-05-19 baseline was n=49, +124.69 %, 51.0 %; the jump reflects the 2026-05-24 `--force` retrain on the freshly-rotated semis/AI-heavy active set).
- **Raw** (toggle OFF, multi-run history): every weekly `--force` retrain stacks a fresh batch on top of the previous.  Answers "how has the strategy behaved across all model versions ever trained?"  2026-05-24 baseline (strategy-decided, fold_end excluded): **n=932, cumulative excess = −1173.30 %, win-rate-vs-benchmark = 34.0 %** (2026-05-19 baseline was n=839, −1711.41 %, 32.4 %; the cumulative-excess number is moving less negative as the new run dilutes older folds' drag).  Cumulative excess here is the unweighted sum of per-trade `excess_pct` × 100 (see `dashboard/pages/10_Trade_History.py:327` — `strategy_df["excess_pct"].sum() * 100.0`).  Not a portfolio-weighted compound return — every trade contributes its raw excess regardless of size or capital, so the magnitude scales linearly with trade count.  Pinned by `tests/test_trade_log.py::test_benchmark_aggregates_raw_baseline_2026_05_24` (`pytest.approx(-1173.30, abs=2.0)`).

The two answers diverge by ~1711 percentage points on the same fold_end-excluded slice (2026-05-24; was ~1836 on 2026-05-19) — neither is wrong.  Page 10 defaults to the deduped view to match the existing summary cards' surface, but the toggle is intentional so an operator who wants the multi-run perspective can see it.  The −7 % avg-excess **stop bleed** is the only finding that survives both views (n=20 deduped, n=526 raw on 2026-05-19 — same magnitude, same sign).  When tuning anything that touches dedup logic, the backfill, or SPY ingestion, run `tests/test_trade_log.py::test_benchmark_aggregates_*_baseline_2026_05_24` as a canary — those tests will fail loudly on drift and require an eyeball check before re-pinning.  The pin date in the test name bumps every Sunday `--force` retrain (followups.md tracks the re-pin as a single-run gate).

**Benchmark-relative tracking uses raw SPY return vs net trade P&L** (`data/ui_queries.py:query_benchmark_returns`, `scripts/backfill_benchmark_returns.py`).  The asymmetry is deliberate:
- The trade's `pnl_pct` is **net** of commissions and slippage (Phase A's `pnl is net` schema convention — see `_close_trade` and the dedicated architectural-decision note above).
- The benchmark's `benchmark_return_pct` is **raw**: `(SPY_close_exit / SPY_close_entry) − 1`.  No fee adjustment.

This is the correct retail-alpha frame: the counterfactual is "would I have done better holding SPY?" — and a buy-and-hold SPY position incurs no trading fees, so the comparison "my P&L net of friction vs frictionless benchmark" is exactly what the retail investor faces.  Computing `excess = pnl_pct − benchmark_return_pct` does NOT need any cost adjustment.  The double-count footgun to watch for: `excess = (pnl_pct − costs_charged) − benchmark_return_pct` subtracts costs *again* from a `pnl_pct` that already excluded them.  Pinned by `tests/test_ui_queries.py::test_excess_return_uses_net_pnl`.

**Page 9 cross-references live IBKR open orders so cancelled bracket legs don't render as if they were still alive** (`dashboard/pages/9_Account.py:_enrich_positions`). `get_latest_risk_levels` reads the *original* bracket prices from `order_decisions` (snapshot of what the runner placed at entry) — but those prices can be stale: once `TrailingStopManager` converts a bracket TP→TRAIL in Phase 3.5, the original LMT and STP have been cancelled at IBKR. Without the cross-ref, the page rendered the cancelled $159.44 TP and $123.13 stop as if they were still protecting SNOW — the 2026-05-19 stale-TP report that triggered this fix. The function now takes `open_orders` (passed from the same `_fetch_ibkr_data` call that supplies positions) and, for each position, checks whether a matching SELL LMT (active TP), SELL STP / STP LMT (active stop), and SELL TRAIL (trailing) exists. When a bracket leg is *not* in `open_orders`, the corresponding column is blanked (rendered as "—" downstream via `na_rep`). The TRAIL path surfaces `trail_amount` (= `auxPrice`, the trail *distance*) and best-effort `trail_trigger` (= ib_insync's `Order.trailStopPrice`, the live ratcheting stop — `None` until IBKR sends the first ratchet update; the card renders "trigger pending IBKR update" in that case). Backward compatibility: passing `open_orders=None` skips the cross-ref entirely (no information to disprove the leg) and the function preserves its legacy behaviour — protects callers like tests that don't supply orders, and tolerates a transient IBKR-orders fetch failure without nuking the risk-level display. New `IBKRConnection.get_open_orders` field `trail_stop_price` exposes `Order.trailStopPrice` for this purpose; the existing `stop_price` field still carries the trail *distance* on TRAIL rows (renamed conceptually but kept for back-compat with the Open Orders table's `Trail $X.XX` formatter).

**Intraday Phase 3.5 reads price from IBKR, not the cached daily bar** (`scripts/intraday_check.py`, `risk/trailing_stop.py:manage(price_source=...)`).  The daily 09:35 ET signal_runner calls `TrailingStopManager.manage()` with no `price_source` and the manager reads the latest bar from `ohlcv_bars` via `get_bars(..., limit=1)`.  That's the existing daily path — unchanged by this work.  The intraday runner at 12:00 ET / 15:30 ET cannot use that path because the same `ohlcv_bars` row is yesterday's close mid-day (18+ hours stale), so it passes a `price_source` callable that wraps `IBKRConnection.get_last_price()` (the 3-tier live → 15-min delayed → yfinance fallback).  The manager calls it once per evaluated long position.  Without this, ratchet detection compares the live `Order.trailStopPrice` against a stale-bar-derived position price — false positives in either direction.  ATR continues to come from `indicator_snapshots` regardless because ATR is a daily-bar-derived value and doesn't change intraday; the trail distance set on conversion is therefore "ATR-as-of-last-completed-bar", not "ATR-as-of-now".  See the `Optional[Callable[[str], float]]` parameter docstring on `manage()` for the contract.  Backward-compat: `price_source=None` (the daily-runner default) preserves the legacy DB-read path exactly — pinned by `test_trailing_manager_default_uses_db`.

**Intraday TP→TRAIL conversion is gated behind a separate config flag and a buffer above the daily-Phase-3.5 activation threshold** (`config/settings.py` → `RiskConfig.intraday_trail_conversion_enabled` + `intraday_conversion_buffer_atr`).  The daily Phase 3.5 at 09:35 ET converts bracket TPs into trailing stops once `current_price >= entry + activation_atr × ATR`.  The intraday runner could in principle do the same at 12:00 / 15:30 ET — but two arguments against making it the default: (1) the cancel-TP → cancel-STP → submit-TRAIL sequence leaves a sub-second "no stop" window that is acceptable at 09:35 ET (lower volatility, opening-auction price discovery still settling) but riskier mid-session when liquidity has thinned and moves are faster; (2) anything close to activation at 12:00 ET was *just* evaluated at 09:35 against the same daily ATR — the marginal cases are by definition the worst-positioned ones for a mid-day decision.  Default is therefore opt-in: `intraday_trail_conversion_enabled=False` ⇒ intraday runs only emit `RATCHETED` / `SKIPPED` rows; no conversions attempted.  When operators flip it on, the activation threshold tightens by `intraday_conversion_buffer_atr × ATR` (default 0.5) on top of the daily `activation_atr` — so a position that would clear $102 (activation) at 09:35 must clear $103 at 12:00.  The buffer keeps mid-day conversions to genuinely-strong moves where the no-stop-window risk is least likely to bite.  Symmetric `intraday=True` kwarg on `manage()` engages the gate; the daily runner never passes it, so the legacy path is unaffected.

**Intraday runner exits 0 on Gateway-down rather than raising** (`scripts/intraday_check.py:run` + `main`).  When `IBKRConnection.connect()` fails (or `IBKRConnection()` itself raises during construction), the runner writes a `status='gateway_down'` row to `intraday_run_log`, logs a WARNING, prints a marked stdout line, and **exits with code 0**.  Same behaviour at the outer `main()` `try/except` — any unhandled exception writes a `status='error'` row and still exits 0.  Why: Windows Task Scheduler treats non-zero exits as failures and applies its retry policy.  IB Gateway logs itself out overnight on this account; a typical morning has a non-zero chance of Gateway being unreachable at the 12:00 ET slot.  A runner that exits 1 on that condition would cause Task Scheduler to retry against an already-flaky gateway, potentially spinning into a noise storm.  A runner that exits 0 leaves Task Scheduler quiet but writes a row that makes the missed run visible on Page 8 the next morning (silent skip ≠ invisible skip).  The exit-0 invariant is defended in three places: (a) the gateway-down branch in `run()` writes its row inside its own `try/except` so a DB failure doesn't propagate; (b) the outer `main()` `try/except BaseException` writes a row and returns 0; (c) the fallback `print` in (b) is ASCII-only so even a `_force_utf8_streams()` failure can't re-raise.  Pinned by `test_intraday_runner_gateway_down_exits_clean` and `test_intraday_runner_top_level_exception_writes_error_row`.

**IBKR `reqExecutions` retention is shorter than documented** (`execution/ibkr_connection.py:get_executions`, `execution/reconciliation.py`, Phase B).  IBKR's docs describe ~7 days of server-side execution history, but the 2026-05-29 Phase B verification found this is a **soft maximum, not a guarantee**: a SCHW stop fill from **2026-05-27 (only 2 days prior) was already absent** from `reqExecutions`, while 5/21 fills (AON/SNOW) were long gone.  The effective window may depend on account type, paper-vs-live, or server-side housekeeping we can't observe.  **Operational consequence:** the daily reconciliation in `signal_runner` Phase 1 must run **reliably every weekday**, not best-effort — a skipped run is *not* recoverable later, because any fill that both opened and closed inside the skipped gap will have aged out before the next reconciliation sees it.  This is a scheduling-reliability requirement, satisfied by the existing `run_daily.bat` Windows Task Scheduler entry; it is **not** a code requirement.  Do not assume the 7-day window is forgiving when reasoning about missed runs or backfill windows.  (The four 5/21–5/27 invisible exits that motivated Phase B were themselves unrecoverable for this reason — see CHANGELOG.md 2026-05-29.)

**`get_executions` requests IBKR's full retention and bounds client-side — no server-side time filter** (`execution/ibkr_connection.py:get_executions`).  `ExecutionFilter.time` is too brittle to rely on: a bare `'yyyymmdd HH:MM:SS'` string triggers IBKR **warning 2174** ("submitted request without explicit time zone … implied time zone functionality will be removed") and applies an *implied* timezone that silently shifts the window; the `'yyyymmdd-hh:mm:ss UTC'` form IBKR's own warning *recommends* was observed (2026-05-29, this Gateway build) to **silently return zero rows**.  So `get_executions` passes an **empty `ExecutionFilter()`** — IBKR caps retention at ~7 days regardless (see the note above) — and the reconciler bounds/aggregates client-side on the normalised UTC `exec_time`.  **Do not "optimize" this by re-adding a server-side `time` filter** to reduce payload size: the payload is tiny (a handful of fills), and the filter's failure modes are silent (wrong window or zero rows) rather than loud.  `_to_naive_utc` (in `execution/reconciliation.py`) asserts/coerces UTC before stripping tzinfo so the watermark comparison stays correct.

**Distributional patterns observed across many trades** — patterns that don't fit case studies (no single-trade narrative), Outstanding bugs (no fix in flight), Enhancements (no forward-looking direction), or followups (not a single-run gate) — are tracked under `docs/findings/`. See `docs/findings/README.md` for the scope rule, document structure, and lifecycle. Current findings: `stop_bleed.md` (2026-05-19, observed/hypothesized), `tp_concentration.md` (2026-05-19, observed/hypothesized).

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

Once a fix is verified live (the user confirms it worked end-to-end in production), move the entry into `CHANGELOG.md` at the repo root — a new dated section with the original entry text, the existing `Status:` paragraph, and a new `Verified:` paragraph naming the run that confirmed it. Until verified, the entry stays here under the "code complete" marker so future agents know it's a closed loop awaiting a real-world signal.

### Outstanding bugs (from 2026-04-18 audit — pending fix)

**Correctness — high priority:**
- **Between-run bracket/trail/TP fills are invisible to all local tables until Phase B reconciliation ships** *(escalated 2026-05-29 — 4th confirmed instance)* (`scripts/signal_runner.py` Phase 2 held-only set, `data/ui_queries.py`, `trade_log`): GTC bracket legs (STP/LMT/TRAIL) fill between daily runs at IBKR, but nothing in the codebase ingests `reqExecutions`, so a filled exit leaves no `CLOSED_LONG`/`order_decisions`/`trade_log` row — the position simply disappears from the next run's IBKR positions list. The daily-review position-diff is currently the *only* signal these exits happened, and it can only *infer* price/reason from OHLCV bars, not confirm shares/fill price/realised P&L. Four confirmed instances in 9 days across all three exit mechanisms: AON 2026-05-21 (trail), SNOW 2026-05-21 (trail), SCHW 2026-05-27 (fixed stop, −5.7%), SPY 2026-05-29 (TP, +2.39%) — none reconstructable from local DB. Impact: realised P&L, win rate, and realised-Kelly inputs are all blind to every winning TP and losing stop that fires off-cycle, which is the majority of exits. **Fix:** ship Phase 4.5 Phase B polling reconciliation (`reqExecutions` at the start of each `signal_runner` Phase 1 → `fill_log` ingest → `trade_log` round-trip aggregation per the documented Phase B schema), which tolerates Gateway downtime by design and back-fills the 7-day execution window. This escalation moves Phase B from "next major roadmap item" to "priority." See the "Phase 4.5 — Realised P&L plumbing" enhancement entry below for the full Phase B schema + reconciliation flow.
- **Trail manager implicitly uses intraday quotes when Phase 2 refresh runs mid-morning** *(partial fix 2026-05-20 — intraday runner shipped option (c); daily-path fix deferred)* (`risk/trailing_stop.py`, `scripts/signal_runner.py:_phase2_data_refresh`): Phase 2 calls yfinance at ~10:06 ET, after the regular session has opened, and writes the live intraday quote as today's "1d close" in `ohlcv_bars`. Phase 3.5 then reads `get_bars(symbol, "1d", limit=1)` and uses that intraday value as the price input to the activation check. The trail manager's docstring (and CLAUDE.md "Trailing stops run as Phase 3.5") imply EOD-close semantics, but in practice the input is an intraday print. **Observable consequence:** all three trailing-stop conversions in system history performed their activation check against Phase 2's partial-bar close, not a prior-day EOD close — SNOW 2026-05-07 ($154.81 morning print), AON 2026-05-19 ($327.39 morning print, ~$0.88 above activation), ASTS 2026-05-20 ($87.28 morning print, $3.22 above activation). Had Phase 2 not run on any of those mornings, all three would have evaluated against the prior-day close and the trail would have stayed SKIPPED — for ASTS specifically, 5/19's close of $80.57 would have left it below activation $83.69. The trade still exits via SELL signal in Phase 4 either way for ASTS, but the framing matters: trail activation today is implicitly an intraday-driven event masquerading as EOD logic, not by design. **Fix considerations:** (a) document the current behaviour explicitly in CLAUDE.md and `trailing_stop.py` docstring (cheapest); (b) refactor the trail manager to read the *prior* daily bar via `get_bars(symbol, "1d", limit=2)[0]` so EOD-close semantics are enforced (more conservative; would have left all three conversions SKIPPED until the next morning's evaluation after `run_eod.bat` writes the finalised close); (c) move trail evaluation explicitly to an intraday cadence (the "Intraday lightweight runner" enhancement) so the input timing is by design.  **Status (2026-05-20):** intraday runner shipped option (c) — `scripts/intraday_check.py` evaluates trailing stops at 12:00 ET / 15:30 ET against `IBKRConnection.get_last_price()` (NOT the cached daily bar), so the intraday codepath has correct-by-design input timing. The daily-runner codepath at 09:35 ET still reads from `ohlcv_bars` and still inherits the Phase-2-partial-bar issue described above — that codepath is unchanged.  **Daily-path fix deferred** between option (b) [enforce EOD semantics via the new `price_source` hook by passing a "prior-day close" callable from the daily caller] and option (d) [remove daily-runner trail evaluation entirely; intraday slots own it].  Option (d) only becomes safe once the intraday runner has demonstrated operational reliability — otherwise removing the daily path risks leaving trail evaluation gapped on intraday-failure days.  **Trigger to revisit (daily-path fix):** after ≥4 weeks of operating the intraday runner.  If `intraday_run_log` shows `status='completed'` runs reliably (say >80% of scheduled slots), option (d) becomes attractive — single canonical path, removes the buggy code instead of working around it.  If intraday Gateway-down events are common, option (b) is the safer pick — leaves the daily-runner trail evaluation in place but anchors it to a non-stale price input.  Don't lock in either now; the choice is a function of operational data we don't have yet.
- **Circuit breaker is effectively manual-only** *(code complete 2026-04-28 — awaiting live verification)* (`risk/circuit_breaker.py`, `signal_runner.py`): `check_loss_limits(daily_pct, weekly_pct)` requires the caller to pass realised loss percentages, but nothing in the codebase computes them. `signal_runner._phase1_startup` should pull `realized_pnl + unrealized_pnl` from `IBKRConnection.get_account_summary()` (and a cached equity baseline) and invoke the check before any signals are processed. Without this, the CB only trips when a human clicks "Trigger" on Page 8. **Status:** implemented via new `equity_snapshots` table + `_check_loss_limits_against_baseline` helper called from Phase 1 when not dry-run. Unit tests pass (5 new in `test_signal_runner.py`). Verification pending: first live `--no-dry-run` run only seeds the baseline (no comparison yet), so the auto-trigger genuinely activates from run #2 onward — needs ≥2 consecutive paper-trading runs to confirm the daily-pct math, plus a deliberately-induced loss to exercise the actual halt path end-to-end.
- **Kelly disconnected from realised outcomes** *(code complete 2026-05-07 — awaiting live verification)* (`risk/position_sizer.py`): Kelly fraction was derived from `|ensemble_score|` as a P(win) proxy; the entire risk stack never saw actual fills or P&L. **Status:** addressed by Phase 4.5 Phase C — see "Status (Phase C — 2026-05-07)" under the Phase 4.5 entry below for the full implementation. `compute_realised_kelly` now reads `trade_log.pnl_pct` for empirical win-rate / avg-win / avg-loss; `PositionSizer` engages the realised path at `n_trades >= min_trades_for_realised_kelly` (default 30) with a cold-start proxy fallback. Verification pending: Sunday `--force` retrain with at least one symbol clearing 30 trades to observe `method='kelly_realised'` actually drive a non-fallback share count in `trade_log`. Live (`source='live'`) Kelly is gated on Phase B and starts tracking realised broker fills the moment that lands.
- **Stale-price signals accepted** *(code complete 2026-04-28 — awaiting live verification)* (`signal_runner.py` + `risk/order_manager._get_latest_close`): `get_bars(..., limit=1)` returns the most recent cached bar regardless of age. If the pipeline hasn't run for a week, signals fire against week-old prices. Fix: gate each symbol on `latest_bar_age < max_stale_days` (config) before passing to `OrderManager.process`. **Status:** implemented via new `RiskConfig.max_bar_staleness_days` (default 3) + a stale-bar gate at the top of `_phase3_signals` that drops symbols whose newest cached daily bar is older than the limit. `skipped_stale` count surfaces in Phase 5 print + `signal_runner_log.skipped_stale`. Unit tests pass (2 new in `test_signal_runner.py`: fresh-pass and stale-drop). Verification pending: needs a real-world run after a deliberate pipeline-skip (e.g. one weekend without `run_pipeline.py`) to confirm the gate fires correctly against actual stale data and that the dashboard shows the skipped count.
- **FinBERT weight floor defeats coverage scaling** *(code complete 2026-05-01 — awaiting live verification)* (`models/ensemble.py` rebalance): after multiplying FinBERT weight by `finbert_coverage`, the 10 % `ensemble_weight_floor` is reapplied unconditionally. A symbol with 0 % coverage still ends up with ≥ 10 % FinBERT weight. Fix: skip the floor when `finbert_coverage == 0`, or apply the floor only to LSTM/XGBoost. **Status:** `_normalise_weights` now floors only LSTM and XGBoost; FinBERT weight passes through untouched so the coverage scaling in `rebalance` step 2 governs it across the entire range (not just at coverage=0). No new tests — the existing `test_models.py` ensemble suite covers the rebalance/normalise flow and continues to pass (146/146). Verification pending: next weekly `--force` retrain — observable in `ensemble_weight_history`, where a 0-coverage symbol's row should now show `finbert ≈ 0` (post-normalisation, ratios depend on LSTM/XGB) instead of `finbert ≥ 0.10`. Low-coverage symbols (e.g. coverage=0.20) should also show proportionally lower FinBERT weight than before.
- **FinBERT `published_at` type assumption** *(code complete 2026-05-12 — awaiting live verification)* (`models/finbert_model.py:123`): `[a for a in articles if a["published_at"] <= now]` assumes every source returns a `datetime`. ORM reads do, but `NewsClient` has three fallback providers (IBKR / Alpaca / yfinance) — confirm each hands off a `datetime` before reaching this filter, or coerce with `pd.to_datetime(a["published_at"])` to be safe. Silent wrong comparison under Py3 is the concern; TypeError is possible if any provider returns a string. **Status:** new `FinBERTModel._coerce_published_at(value)` staticmethod runs every article through `pd.to_datetime(..., errors='coerce')`, strips tz to match the tz-naive pipeline convention (DB + `now`), and returns `None` on NaT / unparseable input — unparseable rows are dropped rather than poisoning the weighted average. Applied at two sites in `_aggregate_sentiment`: (a) after `get_recent_news` returns DB rows, (b) after the live `_news_client.fetch_news` path (which bypasses the DB and so wasn't covered by the first pass). Test coverage: 2 new in `tests/test_models.py::TestFinBERTModel` — `test_coerce_published_at_handles_all_provider_types` exercises every observed input (tz-naive `datetime`, ISO strings with/without offset, tz-naive/tz-aware `pd.Timestamp`, `None`, `pd.NaT`, raw garbage), and `test_aggregate_sentiment_filters_string_published_at_without_typeerror` is the regression guard pinning the original "TypeError: '<=' not supported between str and datetime" failure. Full suite: 206/206 passing + 1 skipped. Verification pending: the next live IBKR-news fetch (any `run_pipeline.py` run that hits the Alpaca fallback tier — currently ~56% of universe symbols per the 2026-05-03 weekly run) is what exercises the live-fetch coercion path against real ISO-string input; before this fix, those symbols' walk-forward FinBERT contributions would have either crashed or silently mis-ordered.
- **Non-Wilder ADX** *(code complete 2026-05-12 — awaiting live verification)* (`models/regime_detector.py:_compute_adx` — the original entry pointed at `data/indicators.py` but ADX actually lives on `RegimeDetector` as a private staticmethod, no separate indicator column): ADX used `ewm(span=period, adjust=False)` which gives α=2/(N+1)≈0.133 for N=14 — roughly twice Wilder's α=1/N≈0.071. Effect: ADX reacted too fast, climbed above 25 more readily than a textbook ADX, biasing the regime detector toward TRENDING classifications. **Status:** all four `ewm` calls (TR→ATR, DM+→DI+, DM-→DI-, DX→ADX) switched from `span=period` to `alpha=1/period`. Test coverage: 2 new in `tests/test_models.py::TestRegimeDetector` — `test_adx_uses_wilder_smoothing` pins the smoothing factor by recomputing the expected value via the Wilder formula and asserting `_compute_adx` matches; `test_adx_strong_trend_exceeds_threshold` confirms a clean monotonic uptrend still produces ADX > 25 after the slowdown. Full suite: 199/199 passing. **Interaction with the SELL-bias mitigation (Option A+C, 2026-05-11):** Wilder is slower-changing, so post-fix more bars classify as MEAN_REVERTING (where the XGB-halving / 1.2× threshold adjustments don't engage). Could partially undo the SELL-bias correction in mixed regimes — watch the next daily run's BUY:SELL ratio. If it regresses meaningfully from the Option A+C baseline, consider lowering `_ADX_TRENDING` from 25.0 (e.g. to 20.0) to recover the prior TRENDING-classification frequency under Wilder's smoother values. Verification pending: compare next daily run's TRENDING-vs-MEAN_REVERTING distribution against the pre-fix baseline using `signal_log.regime` counts.
- **No HOLD-timeout exit rule** *(code complete 2026-05-19 — awaiting live verification)*: once a BUY fills, the position sits until an explicit SELL signal fires. In sparse-signal regimes a position can hold indefinitely. Fix: add a config-driven "flatten after N bars without a re-confirming signal" rule in `signal_runner.py`, or a time-based stop alongside the ATR stop. **Status:** implemented as **Phase 3.6** in `scripts/signal_runner.py:_phase3_6_hold_timeouts`, sequenced after Phase 3.5 (trailing stops) and before Phase 4 (new orders). The "re-confirming signal" semantic was picked over a pure time-based stop: each held long is checked against `signal_log` for the most recent passed-gate BUY via the new `data.database.get_latest_buy_signal_ts(symbol)` helper; if the latest BUY is more than `config.risk.max_hold_days` calendar days old, the position is flattened with a market sell after cancelling its bracket children (LMT TP, STP, STP LMT, and TRAIL legs — same pattern as `OrderManager._cancel_bracket_children` to prevent orphaned stops firing against zero shares and opening unintended shorts post-close). Symbols with NO BUY history in `signal_log` are explicitly skipped (manual positions / pre-history holdings have no anchor for "stale"). Two new `RiskConfig` knobs gate the feature: `hold_timeout_enabled: bool = False` (opt-in, mirrors `trailing_stop_enabled` pattern) + `max_hold_days: int = 30` (~22 trading days; 0 short-circuits as a defensive guard). Phase 3.6 is skipped entirely in dry-run, when `paper_orders_enabled=False`, or when either knob disables it. Each closure is persisted to `order_decisions` with `decision='CLOSED_TIMEOUT'` so Page 8 can surface a retrospective view alongside CLOSED_LONG / APPROVED rows. New `signal_runner_log.hold_timeouts` column (idempotent `_migrate()` ALTER) tracks the per-run count and is printed in Phase 5. Test coverage: 8 new in `tests/test_signal_runner.py::TestHoldTimeout` — disabled-by-config / dry-run / paper-disabled / zero-max-days / recent-BUY-blocks-timeout / stale-BUY-triggers-close-and-persist / no-BUY-history-skipped / shorts-and-flats-filtered. Full suite: **231 passed + 1 skipped** (was 223 + 1 baseline). Verification pending: (a) flip `hold_timeout_enabled=True` in `config/settings.yaml` (Page 5 Risk tab) — the next daily run prints `=== Phase 3.6: Hold timeout ===` and reports `Hold timeouts: 0` in Phase 5 if no positions are stale; (b) a deliberate stale-hold test — set `max_hold_days=1` against a long with a >1-day-old last BUY in `signal_log`, confirm `order_decisions.decision='CLOSED_TIMEOUT'` row appears and IBKR position is flattened; (c) verify the bracket-children cancellation path doesn't strand orphan TRAIL / STP legs (grep daily log for the `Cancelled bracket child` lines that precede the market close).
- *(retired 2026-05-08 — see CHANGELOG.md: "Universe rescore can orphan held positions" + "`signal_log` not populated by daily runner")*

**Performance / hygiene — lower priority:**
- **Row-by-row upserts** in `data/database.py` helpers (`upsert_bars`, `upsert_indicators`, `upsert_news`): each row is a separate transaction. For a 365-bar backfill × 10 symbols this is ~3500 round-trips. Fix: switch to `INSERT ... ON CONFLICT DO UPDATE` with `executemany` or SQLAlchemy's bulk upsert.
- **Event loop per order call** (`risk/order_manager.py`): `_submit_bracket_order` / `_submit_market_close` each create and close a fresh event loop. Fine in single-threaded `signal_runner.py` but risks `RuntimeError: Event loop is closed` from lingering ib_insync callbacks. Related: `signal_runner.py` could be moved to async and reuse a single `IBKRConnection` for the whole run instead of opening/closing per order.
- **Sharpe annualisation hardcoded** *(code complete 2026-05-19 — awaiting live verification)* (`models/walk_forward.py:_compute_fold_performance`; the original entry pointed at `data/walk_forward.py:compute_metrics` but that helper was already parameterised — the remaining hardcode lived on the ML orchestrator's internal helper): uses `√252` regardless of bar interval. Fine for daily, wrong for 1h/15m. Fix: parameterise via `bars_per_year` on `WalkForwardSplit` (252 for 1d, ~1638 for 1h US session, etc.). **Status:** new module-level constant `_BARS_PER_YEAR_DAILY = 252` plus a `bars_per_year: int = _BARS_PER_YEAR_DAILY` parameter on `MLWalkForwardOrchestrator.__init__` (stored as `self._bars_per_year`) and on `_compute_fold_performance(..., bars_per_year=...)`. The orchestrator threads its instance attribute into the staticmethod call. Both hardcoded `252` instances at the original site (`ann_r` CAGR exponent + `sharpe` annualisation factor) now use the parameter. Default preserves current daily behaviour exactly — no caller change needed for `run_daily.bat` / `run_weekly.bat` / `train_models.py` (verified via `test_default_bars_per_year_is_252`, which reconstructs the prior formula and asserts equality). Documented hourly value (~1638 = 252 × 6.5 RTH hours) and 15m value (~6552 = 252 × 26) in the docstring for future use. Test coverage: 5 new in `tests/test_walk_forward.py::TestComputeFoldPerformanceAnnualisation` — default-is-252 (regression guard), hourly-scales-Sharpe, bars_per_year-threads-through-CAGR (asserts both daily and hourly cases against hand-computed values + the ordering invariant), orchestrator-threads-bars-per-year, orchestrator-default-matches-module-constant. Full suite: 236 passed + 1 skipped (was 231 + 1 baseline). Verification pending: this fix is invisible on current daily runs by design — exercising it requires a non-daily WF run, which doesn't exist yet. Live verification gate = the first 1h or 15m WF run (which would need additional plumbing on `train_models.py` / `signal_runner.py` to actually invoke). For now, the regression test pins the daily behaviour and the hourly test pins the parameterised behaviour; either failing would surface in the next pytest run.
- **IBKR error 10148 logged at ERROR for benign duplicate cancel** *(code complete 2026-05-19 — awaiting live verification)* (`execution/ibkr_connection.py:672-684`): the
  `informational` set in `_on_error` does not include code 10148, so "OrderId N that needs to be cancelled cannot be
  cancelled, state: Cancelled" surfaces as ERROR even though the new order (TRAIL or market close) placed alongside
  still succeeds. Today's daily run (2026-05-19) logged it twice — AON trailing-stop conversion (cancel order_id=127
  → place TRAIL id=167 succeeded) and MMM long-only close (cancel order_id=134 → place MKT id=170 succeeded).
  2026-05-07 SNOW first trail conversion logged it once (order_id=106). All three are the same race: a cancel request
   lands against an already-Cancelled bracket child during the cancel+place flow. **Fix:** add `10148` to the
  `informational` set alongside the existing `202` ("Order Canceled" confirmation) and update the "Informational
  error codes continue to expand" note in CLAUDE.md to list the new full set. **Status:** `10148` added to the
  `informational` set in `execution/ibkr_connection.py:_on_error` with an inline comment explaining the cancel+place
  race that produces it. The "Informational error codes continue to expand" architectural-decision note in CLAUDE.md
  was updated to mention 10148 and the full current set is now
  `{2104, 2106, 2107, 2119, 2158, 300, 399, 10148, 10167, 10197, 10349, 202}`. No new tests — `_on_error` is a
  pure log-level routing function with no return value, no side effects beyond logging, and no behaviour worth pinning
  beyond visual log inspection. Verification pending: next daily run that performs a trailing-stop conversion or
  long-only close where the bracket child has already been auto-cancelled by IBKR. Today's run (2026-05-19) had two
  such events (AON / MMM); the next one should show the `10148` line at DEBUG instead of ERROR. Easiest spot-check:
  `grep "10148" logs/daily/daily_run_YYYYMMDD.log` returns lines starting with `DEBUG`, not `ERROR`.
- **Stale partial-day bars in `ohlcv_bars` for symbols outside the active universe** *(code complete 2026-05-19 — awaiting live verification)* (`data/fetcher.py`,
`scripts/signal_runner.py:_phase2_data_refresh`): `signal_runner.py` Phase 2 fetches each symbol's daily bar
mid-day (~10:00-11:00 ET) via yfinance, which returns whatever the day's *partial* state is at fetch time.
`upsert_bars` writes that partial bar to SQLite. Nothing re-fetches the bar after the 16:00 ET close. Symbols that
drop out of the universe AND are no longer held don't get their D-1 bar refreshed at all — so the recorded "daily
low/high/close" remains the mid-day snapshot forever. Concrete observation 2026-05-19: yfinance shows TMUS
2026-05-15 low $185.10 (DB had $186.77 — above its $185.38 stop); UAL 2026-05-18 low $91.36 (DB had $93.79 — above
its $91.29 stop); TEL 2026-05-18 low $197.90 (DB had $200.80 — above its $199.01 stop). All three positions
actually stopped out intraday at their true daily lows, but the DB hid this evidence and caused the
`docs/case_studies/losers_2026-05.md` original §2 framing of TMUS' exit as "mechanism unclear" — see §7 of that doc
  for the full reconstruction. Downstream impact: dashboard Page 1 / 6 / 10 show stale prices for rotated-out
symbols; walk-forward retrains train on stale recent bars; case studies are systematically misled. **Fix:** add an
end-of-day refetch pass — new script `scripts/refresh_recent_bars.py` (cron at 16:30 ET on weekdays, or a fourth
step in `run_daily.bat`) that re-fetches the last 2 trading days' bars for the union of (currently-held symbols,
symbols held in the last 14 days, current universe). Overwriting on upsert is already the behavior; the gap is just
  that nothing currently triggers a re-fetch after close. ~50 lines, ~5-10 sec runtime for the current universe
size. **Status:** implemented as `scripts/refresh_recent_bars.py` + a new `run_eod.bat` scheduler wrapper. The spec's "overwriting on upsert is already the behavior" turned out to be wrong — `upsert_bars` / `upsert_indicators` both skipped existing rows. Two additions: (a) new `overwrite: bool = False` parameter on both helpers in `data/database.py` (default preserves existing skip-existing semantics for the incremental-fetch path); (b) new `DataFetcher.refresh_recent(symbol, interval='1d', days_back=5)` that calls yfinance and upserts with `overwrite=True`. The script builds its symbol union from three sources: (1) `get_universe_assets(active_only=True)` or `config.data.watchlist`, (2) `order_decisions` with `decision in ('APPROVED', 'DRY_RUN', 'CLOSED_LONG')` and `decided_at > now − 14d` (as a Phase-A proxy for "recently-held" until Phase B lands `source='live'` rows in `trade_log`), (3) currently-held IBKR positions (optional — `--no-ibkr` flag, also degrades cleanly to empty set when Gateway is unreachable). For each symbol it overwrites recent bars then recomputes and overwrites the derived indicators. `run_eod.bat` lives separately from `run_daily.bat` because the daily run fires pre-market at 09:40 ET; EOD wrapper logs to `logs/eod/eod_run_YYYYMMDD.log` and must be scheduled separately via Windows Task Scheduler at 16:30 ET. Test coverage added: 5 new in `tests/test_data_pipeline.py` (`TestUpsertBarsOverwrite` × 3: default skips, overwrite updates in place with the low-overwrite canary, mixed batch handles INSERT+UPDATE in one call; `TestUpsertIndicatorsOverwrite` × 2). Full suite: **223 passed + 1 skipped**. Verification pending: (a) the Phase B `source='live'` lookup in `_recently_acted_symbols` should be switched from `order_decisions` to `trade_log` once Phase B accumulates rows; (b) needs a real EOD cron run to confirm yfinance returns finalised post-close bars at 16:30 ET (yfinance typically updates ~15-20 min after close) and that the held-positions union actually catches the rotated-out symbols that motivated this fix; (c) repeat the TMUS / UAL / TEL spot-check from the original observation against the DB after one EOD run completes — DB Low values should match yfinance Low values to within a cent.

### Enhancements (open)
- **Benchmark-relative performance tracking on Page 10** *(code complete 2026-05-19 — awaiting live verification across next weekly retrain)* (originated from 2026-05-19 chat review): Page 10 previously showed absolute P&L, win rate, and a cumulative net-P&L curve — but a +5 % return when SPY did +6 % is a negative-alpha system that looked like a winner on the dashboard.  **Status:** SPY OHLCV ingestion mirrored on the `^VIX` pattern so it does not depend on universe mode (new `DataConfig.benchmark_symbol`, fetched explicitly in `run_pipeline.py` + `refresh_recent_bars.py` union).  New `trade_log.benchmark_return_pct` column populated by `scripts/backfill_benchmark_returns.py` (idempotent — operates only on `WHERE benchmark_return_pct IS NULL`).  Backfill auto-wired into `run_weekly.bat` after `train_models.py --force` and `run_daily.bat` after `train_models.py`, with a NULL-count verification echo in both.  Page 10 has a new "Benchmark-Relative Performance" section: 3 metric cards, cumulative excess chart, per-exit-reason excess table, per-trade audit expander with opt-in fold_end toggle (default OFF — the headline metrics exclude fold_end backtest artifacts; the toggle affects only the per-trade table).  See the "Fold-end closures are backtest artifacts" and "Dedup vs raw views are honest answers to different questions" architectural-decision notes above for the diagnoses landed during this work.  Test coverage added: 7 new tests across `tests/test_ui_queries.py` (excess-return regression guard, NULL-row filtering, empty-state) and `tests/test_trade_log.py` (backfill correctness, missing-bar NULL handling, idempotency, two 2026-05-19 baseline-pin canaries against the production DB).  Verification pending: (a) next Sunday `--force` weekly retrain will exercise the auto-backfill chain end-to-end and re-pin the baseline numbers — the two `test_benchmark_aggregates_*_baseline_2026_05_19` tests will fail loudly and need updating, that's the canary firing as designed; (b) operator should spot-check Page 10's headline 3-card numbers against the per-exit-reason table totals to confirm the fold_end exclusion is honest.

- **Trailing stop is structurally crowded out by the bracket TP on fast moves** (`risk/trailing_stop.py`, `config/settings.py` RiskConfig): with current defaults (`trailing_stop_activation_atr=2.0`, `trailing_stop_trail_atr=2.0`, `atr_take_profit_multiplier=3.0`), the activation level sits only **1× ATR below** the bracket TP. The trailing manager evaluates **once per day** in Phase 3.5 reading daily-bar closes from `ohlcv_bars`; intraday excursions above activation are invisible to it. On any fast move that gaps through both activation and TP between two consecutive daily evaluations, the bracket TP wins by construction — the trailing stop cannot engage. **Observed case: AXTI 2026-04-29 → 2026-05-04** (full write-up: `docs/case_studies/axti_2026-04.md`); **counter-case where the trail DID convert: SNOW 2026-04-30 → ongoing** (`docs/case_studies/snow_2026-04.md`). The pair refines the framing from "the trail can't engage on fast moves" to "the trail's success depends on whether the move from below-activation to above-TP happens **overnight** (TP wins — AXTI) or **intraday** while the once-per-day manager is running (trail wins — SNOW). The intervention ranking flips: SNOW's data argues **intraday evaluation > widen activation→TP gap > lower activation_atr**." Entry $72.96; 2026-05-01 close $88.32 sat $3.57 *below* activation $91.89 (intraday high $96.00 would have qualified but daily-bar evaluation missed it); 2026-05-04 opened $97.44 and TP $101.19 filled in the first ~10 minutes of trading before the 09:40 ET trailing-manager evaluation. Realised +38.7% in 3 trading days vs subsequent post-exit peak of $134.00 on 2026-05-13 (~46% of peak move captured). Sample size = 1 today; **do not retune defaults until ≥2 more fast-move winners exit and the pattern is confirmed in aggregate** (gated on Phase B `source='live'` rows so realised P&L drives the analysis instead of a single observation). Interventions ranked by reversibility when the time comes: (1) lower `trailing_stop_activation_atr` to 1.0 — cheap config change, would have activated AXTI's trail on 2026-05-01, risk is over-activation on weaker moves that then retrace; (2) raise `atr_take_profit_multiplier` to ~5.0 — gives the trail room to engage, larger drawdown risk on positions that hit the trail and reverse; (3) intraday trailing-stop evaluation (second `signal_runner` invocation at e.g. 15:30 ET, or a live data stream) — would have caught AXTI's 2026-05-01 intraday $96.00 print, larger code surface; (4) "TP becomes trailing once first-target is hit" rule — conceptually cleanest, largest code change. Trigger to revisit: ≥3 trades total with `exit_reason='tp'` AND post-exit peak >1.5× the TP price within 10 bars (i.e. the system locked in a small win and left a big tail behind). The query that surfaces these once Phase B lands is straightforward against `trade_log` joined to `ohlcv_bars`.
- **Intraday lightweight runner — Phase 1 + Phase 3.5 only** *(code complete 2026-05-20 — awaiting live verification)* (new script, e.g. `scripts/intraday_check.py`; pairs with the "Trailing stop crowded out" entry above and the "Wire up `reqPnL` for intraday circuit-breaker triggering" entry below): the current `signal_runner.py` runs once per weekday at 09:35 ET and does everything (data refresh → signals → trailing stops → orders). Of those five phases, only **Phase 1 (circuit breaker / loss-limit check)** and **Phase 3.5 (trailing-stop conversion / ratchet)** genuinely benefit from intraday re-evaluation — both depend on current price, which moves all day. The signal layer (Phase 3) and new-order submission (Phase 4) would produce **identical output** at 12:00 ET as at 09:40 ET because LSTM/XGBoost/FinBERT all infer against the same `ohlcv_bars` row until that bar closes (~16:00 ET); re-running them mid-day would just litter `signal_log` with duplicate rows. Phase 2 (yfinance data refresh) is actively *harmful* mid-day because the partial-day bar is noisier than the previous completed bar. STP and TP legs live continuously at IBKR so they don't need our script either. **What the new runner should do:** open one IBKR connection → run circuit-breaker check against current account P&L → iterate held positions and evaluate trailing-stop activation against `IBKRConnection.get_last_price()` (NOT the cached daily-bar close, which goes stale mid-day) → close connection. Estimated ~30 seconds per invocation, no model loading, no news fetch, no duplicate signal rows. Schedule via Windows Task Scheduler at e.g. 12:00 and 15:30 ET on weekdays (skip the 09:35 slot — the existing `signal_runner.py` already runs then). **Motivating cases (already documented):** SNOW's 2026-05-07 trail conversion happened at 09:47 ET when the manager caught the price at $154.81 between activation $150.83 and TP $159.43 — but it was *one of one* in the entire database history precisely because the once-per-day cadence puts the manager in the right place at the wrong time on almost every fast move (AXTI 2026-05-04 is the matched failure case — see `docs/case_studies/snow_2026-04.md` §6). Separately, UAL bled from -2.7% to -5.4% across 2026-05-11 → 2026-05-15 without any intraday check that could have triggered the circuit breaker on the cumulative loss — see `docs/case_studies/losers_2026-05.md` §3. **Prerequisites and risks:** (a) IB Gateway must be up at the intraday slots — currently it's flaky (often logged out overnight), so the IBC adoption entry below should ship first or in parallel; (b) the trailing-stop evaluation must be modified to accept a price-source override (today it reads `get_bars(..., limit=1)` from the DB; the intraday path needs `IBKRConnection.get_last_price()` instead so it sees current price, not yesterday's close) — `TrailingStopManager.manage()` would gain an optional `price_source` callable parameter, defaulting to the current DB read for backward compat with `signal_runner.py`; (c) the `trailing_stop_log.atr` and `trail_amount` columns become more nuanced — ATR is a daily-bar-derived value, so re-running at 12:00 ET uses the same ATR as the morning run (fine, but document that the trail distance is "ATR-as-of-last-completed-bar" not "ATR-as-of-now"). **What this does NOT need:** intraday model retraining, intraday news fetching, intraday fundamentals refresh, intraday universe rescoring — all of those stay on their existing daily/weekly cadence. **Trigger to ship:** any of (a) one more AXTI-style "TP fired before trail could engage" case lands and the SNOW finding becomes 1-of-3 instead of 1-of-2 (the matched-pair argument strengthens to "the cadence is the bottleneck"); (b) IBC adoption ships (removing the Gateway-uptime blocker); (c) a position bleeds past the daily-loss circuit-breaker threshold *between* daily runs and the next morning's run catches the damage too late. Any of those three makes this a "do it now" rather than "consider it."

  **Status (2026-05-20):** implemented.
  - **What was implemented:**
      * `scripts/intraday_check.py` — new top-level runner.  Event-loop-first IBKR connect (mirrors `signal_runner._connect_ibkr_if_needed`), three internal phases (`_setup_loop_and_connect` → `_phase1_circuit_breaker` → `_phase3_5_intraday_trail`), single IBKR connection reused across both phases.  `_make_price_source(ibkr, loop)` shim wraps the async `get_last_price` for the manager's sync `price_source` contract.  Last-line-of-defense `try/except BaseException` in `main()` always writes a row + exits 0.  `_force_utf8_streams()` reconfigures stdout/stderr at start for ad-hoc invocation outside the batch file.
      * `run_intraday.bat` — schedules `python scripts/intraday_check.py --no-dry-run`.  `HHMM` in log filename so multiple runs per day don't collide.  Two separate Task Scheduler entries (`TradingApp\IntradayMidday` 12:00 ET and `TradingApp\IntradayLateAfternoon` 15:30 ET) point at the same batch file — documented in `README.md` "Creating the scheduled tasks".
      * `risk/trailing_stop.py` — `manage()` gained `price_source: Callable[[str], float] | None = None` and `intraday: bool = False` keyword-only params.  Daily-runner default (`price_source=None`) preserves the existing DB-read path exactly (pinned by `test_trailing_manager_default_uses_db`).  New `_resolve_current_price`, `_safe_float`, `_detect_ratchet` helpers.  New `RATCHETED` action value on `TrailingStopAction` plus a ratchet-detection branch in the idempotency case (compares live `Order.trailStopPrice` against the last logged `current_price − trail_amount`).  Intraday conversion gate at step 2b: when `intraday=True` and `intraday_trail_conversion_enabled=False`, conversions are suppressed before any ATR work (ratchet-only mode).  When enabled, activation threshold tightens by `intraday_conversion_buffer_atr × ATR`.
      * `risk/order_manager.py` — new module-level `flatten_all_longs(ibkr, loop, run_id)`.  Broader 4-type cancel filter (`LMT`, `STP`, `STP LMT`, `TRAIL`) from day one — the existing `_cancel_bracket_children` 3-type bug is intentionally NOT fixed here (separate one-line PR; the new helper is built correct from day one).  Persists each closure as `decision='CB_FLATTENED'` to `order_decisions`.
      * `data/database.py` — new `IntradayRunLog` ORM table.  New helpers `log_intraday_run`, `get_intraday_run_log(on_date=...)`, and `get_latest_trailing_stop_log_for_symbol` (used by ratchet detection).
      * `data/ui_queries.py` + `dashboard/pages/8_Risk_&_Portfolio.py` — new `query_intraday_run_log_today` (ttl=60) + "Intraday checks (today)" section above the CB event log.  Color-coded by status (completed=green, gateway_down=amber, cb_tripped/error=red).
      * `config/settings.py` — two new `RiskConfig` knobs: `intraday_trail_conversion_enabled: bool = False` (opt-in) and `intraday_conversion_buffer_atr: float = 0.5`.
  - **Test coverage added (18 new across 2 files):**
      * `tests/test_trailing_stop.py` — 10 new in 4 classes: `TestPriceSourceParameter` × 2 (default-uses-DB + override-skips-DB), `TestRatchetDetection` × 4 (ratchet detected + no-ratchet on unchanged trigger / no prior row / live-trigger-not-yet-reported), `TestIntradayConversionGate` × 2 (suppressed by default + buffer requirement), `TestIntradayRunLogSchema` × 2 (table exists + round-trip).
      * `tests/test_intraday_check.py` — 8 new in 2 classes: `TestIntradayRunner` × 6 (price-source-uses-IBKR / gateway-down-exits-clean / dry-run-skips-flatten / no-dry-run+tripped-CB-calls-flatten / top-level-exception-writes-error-row / no-baseline-skips-CB), `TestFlattenAllLongs` × 2 (cancels-TRAIL + persists-CB_FLATTENED).
      * Full suite: **278 passed + 1 skipped** (was 252 baseline).  `scripts/signal_runner.py` untouched throughout — daily-runner integration preserved by definition.
  - **Verification pending:** (a) first Task-Scheduler-driven 12:00 ET / 15:30 ET runs in a real session.  Expected first-day signature in `intraday_run_log`: status='completed' rows with `cb_tripped=0`, `trailing_evaluated ≈ count of held longs`, `trailing_ratcheted ≈ 0` for the very first run after the morning conversion (Order.trailStopPrice is None until IBKR sends the first ratchet update — see the `_detect_ratchet` short-circuit).  Over ~1 week of operation, expect to see `trailing_ratcheted ≥ 1` rows once IBKR ratchet updates settle in.  (b) A deliberate Gateway-down test at one of the scheduled slots — leave IB Gateway logged out at 12:00 ET, confirm a `gateway_down` row lands and Task Scheduler reports the task as Succeeded (exit 0).  (c) After ≥4 weeks, evaluate whether to flip `intraday_trail_conversion_enabled` on — the gate to that decision is operator confidence in the runner's reliability, not a metric.
- **Richer cost model**: bid-ask spread, partial fills, market impact in `models/walk_forward.py`.
- **LSTM ↔ MACD direction-disagreement gate** (`models/signal_gate.py`): TEL's 2026-05-07 entry had LSTM +0.976
(strongly bullish) AND MACD -2.76 (negative — bearish direction). The two model views directly contradicted each
other; the gate passed because LSTM dominated the ensemble and 3/3 confirmation was met (FinBERT +0.177 also
bullish). TEL then bled -7% over 7 trading days before the stop fired — see `docs/case_studies/losers_2026-05.md`
§4 for the full holding-period analysis. A simple "block BUY when LSTM > 0.7 AND MACD < 0" rule would have caught
TEL cleanly at entry. **Open design questions before shipping:** (a) does the same rule block too many *winners*?
AXTI's entry-day MACD isn't in `indicator_snapshots` history but post-entry MACD was rising — would have passed.
SNOW's entry MACD likewise needs checking. (b) Direction-disagreement is one specific feature-disagreement gate;
the broader question is whether ANY of the model's input indicators contradicting the LSTM verdict is informative —
  RSI ≥ 70 + LSTM > 0.9 (UAL pattern), MACD < 0 + LSTM > 0.7 (TEL pattern), price > BB upper + LSTM > 0.9 (the
2026-05-11 XGBoost diagnosis pattern) are all candidates. The cleanest first iteration is one feature at a time —
start with MACD because the TEL case is the cleanest in-doc example, instrument the rejection counts via Page 8
(`order_decisions.reject_reason='lstm_macd_disagree'`), and measure rejection vs. realised-outcome correlation once
  Phase B is live. **Defer until** Phase B has ≥10 `source='live'` rows so MACD-at-entry can be cross-tabulated
against actual outcomes; otherwise we're optimising for a single observation. **Trigger to revisit:** a fifth loser
  entry with the same LSTM/MACD-disagreement pattern (would make this 2-of-N), OR Phase B realised-Kelly showing
systematically negative f* on the LSTM > 0.7 + MACD < 0 subset.
- **Point-in-time fundamentals for walk-forward** (`data/fundamentals.py`, XGBoost training pipeline): `FundamentalsClient._fetch_and_cache` calls `yf.Ticker(symbol).info`, which is a *current* snapshot only. The 13 fundamental features fed to XGBoost (P/E, ROE, profit_margin, revenue_growth, etc.) are therefore the *same value* for every historical training bar — a stock that was unprofitable a year ago but profitable today gets today's profit margin applied to last year's bars. Mild lookahead bias today at 120 train bars (~6 months); gets worse as `wf_train_bars` is extended. Symptom to watch for: if a `wf_train_bars` experiment (e.g. 252 instead of 120) shows XGBoost's contribution improving substantially more than LSTM's, suspect this is doing some of the work — LSTM uses no fundamentals so its scaling is "clean". **Options ranked by effort/cost:**
    * **yfinance quarterly statements** (free, already installed): `Ticker.quarterly_financials` / `quarterly_balance_sheet` / `quarterly_cashflow` provide ~5 years of point-in-time quarterly filings. Compute ratios from raw line items (P/E = price ÷ TTM EPS *as of filing date*, ROE = TTM net income ÷ equity, etc.) and join to each bar via the latest filing whose `filing_date <= bar_ts`. Roughly half-day of ETL: new `historical_fundamentals` table keyed on `(symbol, filing_date)`, a join helper, and an `XGBoostModel._features` change to read filing-date-aware values. Risk: yfinance filing dates aren't always reliable; cross-check against the SEC EDGAR `filed` field on a few symbols before trusting.
    * **SimFin free tier** (free, registration required): designed exactly for point-in-time fundamentals research. ~10-line integration, cleaner than yfinance, but adds an external dependency and an API key.
    * **Financial Modeling Prep / Sharadar / Nasdaq Data Link** (paid, $15–50/month): cleanest data, most comprehensive history. Probably overkill for a single-user learning system.
    * **SEC EDGAR XBRL** (free, authoritative): the actual source of truth but raw filings need significant ETL. Skip unless one of the above proves unreliable.
- **Consider walk-forward Sharpe in signal generation** (`scripts/signal_runner.py`, `models/signal_gate.py`, or a new pre-gate filter): today WF Sharpe-per-fold influences signals only *indirectly* — fold-end rebalancing nudges LSTM-vs-XGBoost ensemble weights, then the final retrain on the full dataset produces the live model. Nothing reads `walk_forward_results.sharpe_ratio` at signal-gate time, so a symbol whose most recent fold posted Sharpe = −1.22 (the MMM example that surfaced this, 2026-05-14) can still emit a BUY today as long as the current ensemble score clears the threshold + confirmation filters. Two implementation shapes to consider when this is picked up:
    * **Hard filter** — drop signals from symbols whose latest WF run has aggregate Sharpe (or trailing-fold Sharpe) below a configurable floor. Cheapest to implement, easiest to reason about. Risk: throws away symbols mid-recovery; a single bad fold permanently mutes a name until next retrain.
    * **Soft penalty** — multiply `signal_threshold` by a function of recent WF Sharpe (e.g. raise threshold for low-Sharpe symbols, lower it for high-Sharpe). Smoother behaviour, harder to tune. Could live as a new `_adjusted_threshold` term in `models/signal_gate.py` alongside the regime adjustment.

  **Open design questions** (resolve before code):
    * **Which Sharpe?** Aggregate across the 5 folds (mean / median), trailing fold only, or weighted toward recent folds? Trailing-fold is most reactive but noisiest; aggregate is more stable but slow to react when a symbol's regime shifts. The 5-fold mean from `walk_forward_results` is probably the right default.
    * **Threshold value?** Sharpe = 0 is the obvious zero-skill line, but post-cost WF Sharpes are often negative even for symbols that produce reasonable live P&L (the cost model is conservative — flat slippage + commissions + the bracket simulator now charges stop-slippage too). A more honest floor might be the universe-wide *p25* of WF Sharpe at any given time, not an absolute number. Compute on the fly from `walk_forward_results`.
    * **Survivorship-bias interaction**: when `universe_policy='dynamic'`, the WF Sharpe is biased upward (the universe was selected with today's knowledge). Filtering on biased Sharpe could amplify that bias. The `universe_policy` column (added 2026-05-12) makes this explicit — consider applying the filter only to `static`-policy rows, or applying a separate (looser) threshold to `dynamic`-policy rows.
    * **Pairs with Phase 4.5 Phase C — realised Kelly**: once `source='live'` rows accumulate, *realised P&L* becomes a more honest scoreboard than WF Sharpe (no cost-model approximations, no survivorship). A WF-Sharpe filter is a useful interim signal but the right long-term answer is "filter on realised P&L when n is high enough, fall back to WF Sharpe in cold-start." That's the same cold-start pattern Phase C already implements for sizing — mirror it for filtering.
    * **Observability**: add a new column to `order_decisions` like `wf_sharpe_at_decision` so post-hoc analysis can answer "did the filter drop signals that would have made money?" without re-deriving the value.

  **Defer until** ≥2 weeks of Phase B `source='live'` rows exist — same gate as the IBKR-scanner enhancement above, for the same reason: tuning a Sharpe-floor on WF-only data risks discarding symbols whose live behaviour differs from WF (e.g. the SELL-bias diagnosis from 2026-05-11 — where XGBoost's "high RSI → mean reversion" rule was *correct in WF cost-model terms* but is being overruled by a stronger trend in live markets). A combined WF + live filter is more defensible than either alone.
- **YAML unknown-key warning** *(code complete 2026-05-12 — awaiting live verification)* (`config/settings.py:_apply_yaml_section`): the loader silently ignores YAML keys that don't match a dataclass field, so typos like `min_trades_for_realised_kellly` (three L's, surfaced 2026-05-07 during Phase C verification) disable a config override without warning. The dataclass already has `dataclasses.fields(obj)` available — for each key in the YAML section that isn't in that field set, emit `log.warning("Unknown YAML key: %s.%s — ignored", section_name, key)`. ~10-line change. Bonus: add the same warning at the top level for unknown sections (`config:` typo would silently drop the whole section). Cost is one extra log line per unknown key per process start; benefit is catching every future typo in 30 seconds instead of however long until the symptom is noticed. **Status:** implemented via `warnings.warn(...)` (UserWarning, not log.warning — Python's `warnings` machinery is the right channel for "your config is invalid" since it surfaces on stderr at import time before logging is even configured). `_apply_yaml_section` accepts a `section_name` kwarg and warns per unknown key; `load_yaml_config` warns per unknown top-level section. Test coverage: 5 new in `tests/test_settings.py` (known-field override / unknown-key warns + ignored / mix of known+unknown / no-section-name branch / unknown-section warns). Full suite: 197/197 passing. Verification pending: deliberately introduce a typo in `config/settings.yaml` (e.g. `kelly_fractoin: 0.5`) and confirm the warning surfaces at process start.
- **trade_log.pnl semantics — cross-reference checklist** *(code complete 2026-05-12)* (carry-over from 2026-05-07 Page 10 P&L fix): three semantic flips happened in 8 days around `trade_log.pnl` (Phase A wrote net, Phase C consumed via pnl_pct, Page 10 misread net as gross). The architectural decision note now anchors the convention, but a "if you touch trade_log.pnl semantics, also update" checklist would prevent the next divergence. Touch points to enumerate: (a) `models/walk_forward.py:_close_trade` (the writer), (b) `data/ui_queries.py:query_trade_log` derived columns + `query_trade_summary` aggregation, (c) `risk/position_sizer.py:compute_realised_kelly` (consumer of `pnl_pct`), (d) Phase B reconciliation Pass 2 (when it lands — must follow the same `pnl is net` rule when writing `source='live'` rows from IBKR per-fill `realizedPNL`), (e) `dashboard/pages/10_Trade_History.py` column labels, (f) `tests/test_ui_queries.py::test_net_pnl_equals_stored_pnl` (the canary). Add as a comment block at the top of `walk_forward._close_trade` so it's the first thing anyone editing that function sees. Estimated effort: 15 minutes. **Status:** implemented as a 20-line comment block immediately above `_close_trade` (`models/walk_forward.py:361-383`) — first thing anyone editing the function sees. Lists the convention (`pnl is net`, `gross_pnl = pnl + costs_charged`), the anti-pattern to avoid (`net_pnl = pnl - costs_charged` double-counts), and all 5 touch points to update if the convention ever changes. No code change beyond the comment; nothing to verify live.
- **Retire verified-but-unmarked bug fixes** *(completed 2026-05-11)*: three "code complete — awaiting live verification" entries (datetime.utcnow / dashboard path / LSTM determinism) were silently verified by the daily-run cadence; all three retired to `CHANGELOG.md` on 2026-05-11. The earlier two from the same carry-over (signal_log / universe rescore) were retired 2026-05-08.
- **Survivorship-bias column on `walk_forward_results`** *(code complete 2026-05-12 — awaiting live verification)*: add `universe_policy` ∈ {`dynamic`, `static`} per row so dashboard Page 4 can flag biased runs. Today the warning is only logged. **Status:** ORM column + idempotent `_migrate()` `ALTER TABLE` added (all 3777 existing rows backfilled to NULL on first DB engine init after the migration — verified by hand). `MLWalkForwardOrchestrator.run` now writes `"dynamic"` when `self._universe_selector is not None`, `"static"` otherwise, into the existing `db_record` dict. `query_walk_forward_results` surfaces the field as `"Universe Policy"`. Page 4 (`dashboard/pages/4_Walk-Forward.py`) now: (a) renders a `st.warning(...)` banner above the summary cards whenever any row in the current view has `universe_policy='dynamic'`, listing dynamic/static/unknown breakdown; (b) adds a colour-coded `Universe Policy` column to the detailed results table (amber for `dynamic`, teal for `static`, grey for `unknown`/NULL). Test coverage: 3 new in `tests/test_walk_forward.py::TestUniversePolicyTagging` — orchestrator-default-is-static, orchestrator-with-selector-is-dynamic, ORM-round-trip (writes both policies via `log_walk_forward_result` and reads them back). Full suite: 206/206 passing + 1 skipped. Verification pending: next Sunday `--force` weekly retrain will write the first non-NULL `universe_policy` rows — confirm the value matches the run's source (today `config.universe.enabled=False` so all rows should land as `"static"`); flip `universe.enabled=True` and rerun to see the banner fire on Page 4.
- **Expand `_SECTOR_MAP`** *(code complete 2026-05-12 — awaiting live verification)*: sector exposure check in `PortfolioGuard` currently silently passes unknown symbols. **Status:** the map in `risk/portfolio_guard.py` was expanded from ~46 entries (mostly FAANG + Dow components) to 89 entries covering the entire active universe (verified 100% coverage against `universe_assets WHERE active=1` at the time of the change — all 68 active symbols now resolve to a sector) plus commonly-traded large-caps that have surfaced in `order_decisions` recently (AZN / SNOW / SYY / UAL / TMUS).  Map is now organised by sector with section comments so the next universe addition can find the right bucket fast. Test coverage: 4 new in `tests/test_risk.py::TestPortfolioGuard` (blocks-over-cap / passes-under-cap / cross-sector-isolation / unknown-symbol-skipped — none existed before) plus a `TestSectorMapCoverage::test_every_active_universe_symbol_is_mapped` regression guard that names any unmapped symbols when the universe expands (test is a no-op against the empty test DB; production-DB coverage was hand-verified to 100%).  Full suite: 203/203 passing + 1 skipped (the coverage check on the empty test DB).  Sector labels remain simplified from S&P GICS — "Technology" = Information Technology, "Telecom" = Communication Services, "Consumer Disc" = Consumer Discretionary; matches the existing convention.  Verification pending: the regression-guard test won't fire against production until the universe expands beyond the current 68; the active observable for "did this work?" is `signal_runner_log` showing **non-zero `sector_exposure` rejections** appear in `order_decisions.reject_reason` once a sector hits the 30% cap (today they would have silently slipped through for ~half the universe).
- **Wire up `reqPnL` for intraday circuit-breaker triggering** (`execution/ibkr_connection.py`, `risk/circuit_breaker.py`, `scripts/signal_runner.py`): today the circuit breaker only checks loss limits at Phase 1 startup of `signal_runner.py`, which means an intraday drawdown after the morning run won't trip a halt until the next day's run. IBKR's `reqPnL(account, "")` streams real-time `dailyPnL`, `unrealizedPnL`, `realizedPnL` per account and was confirmed available on this paper account by the 2026-05-02 capability sweep (no extra subscription needed — works on the standard market-data tier alongside delayed quotes). Implementation sketch: (a) add `subscribe_pnl()` / `unsubscribe_pnl()` methods to `IBKRConnection` that wrap `ib.reqPnL` and expose the latest `(daily, unrealized, realized)` tuple; (b) in `signal_runner._phase1_startup`, after the existing baseline check, leave the subscription open for the duration of the run and re-check `circuit_breaker.check_loss_limits` between phases so a fast loss during Phase 3.5/4 halts further submissions; (c) for a longer-lived halt path, the Page 8 dashboard could poll the same subscription via a long-running background process, but that's out of scope for this item — the win is just "the runner itself stops doing damage when losses spike mid-execution". **Pairs naturally with the `Circuit breaker is effectively manual-only` bug** (above) — that entry shipped the baseline+startup-check half; this entry adds the intraday half. Don't ship until that bug is verified live (the entry says "needs ≥2 consecutive paper-trading runs"), since wiring `reqPnL` on top of an unverified baseline-loss path doubles the surface area to debug if something misbehaves.
- **Test IBKR scanners as a Stage 1 universe replacement** (`data/universe.py`, new module e.g. `data/ibkr_scanner.py`): Stage 1 today calls `alpaca.trading.client.TradingClient.get_all_assets()` to enumerate the tradable US equity universe, which is why running `universe_scheduler.py --run-now` requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` env vars (without them `_stage1_fetch` raises `UniverseError` and only the permanent fixtures survive). The 2026-05-02 capability sweep confirmed `reqScannerParameters` returns 477 scan types and 60 instruments on the current subscription (1.7 MB XML, no extra cost) — far broader than expected. Plausible scan codes that overlap Stage 1's intent: `TOP_PERC_GAIN` / `TOP_PERC_LOSE` (momentum-skewed but covers active names), `MOST_ACTIVE` (volume), `HIGH_OPEN_GAP`, `HOT_BY_VOLUME`, plus market-cap and price-range filters via `ScannerSubscription` parameters (`abovePrice`, `belowPrice`, `marketCapAbove1e6`). Approach: (a) prototype an `IBKRScannerClient` that runs 2-3 complementary scans and unions the results into a candidate list; (b) compare the resulting universe against an Alpaca-driven run on the same day for symbol overlap and any structural blind spots (e.g. ETFs, low-volume names that pass Alpaca's `tradable=True` filter but never appear in IBKR scans); (c) decide whether to replace Stage 1 outright, run both as a fallback chain (IBKR first → Alpaca on failure, mirroring the news-tier pattern), or keep Alpaca and use scanners as a separate feature signal. The win if it works: removes the Alpaca dependency entirely (one fewer API key, one fewer external service to monitor), and makes the system pure-IBKR for both data and execution — which simplifies the Stage 1 failure mode that already manifested as "fixture-only universe" in past runs without keys. Risk: IBKR scanners are momentum / activity oriented, so a naive replacement may bias the candidate pool toward high-volatility names; the empirical comparison in (b) is the gate. **Defer until Phase 4.5 Phase B has accumulated ≥2 weeks of `source='live'` rows in `trade_log`** — comparing two universe-selection strategies needs realised P&L as the scoreboard, not WF Sharpe alone. With Phase B now design-staged as polling reconciliation (no IBC dependency), the realistic earliest start for this scanner-test work is roughly mid-June 2026 (assuming Phase B ships late May / early June plus the 2-week accumulation window).
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

- **Automate post-daily-run log review (observability)**: today the `/daily-run-review` skill is invoked manually after each `run_daily.bat` execution — appropriate for the current heavy-development phase where schema and logging conventions are still moving (Phase 4.5 B/C, logging v2, weekly-log trim all open). Automating now would mostly produce noise against a moving target and force ongoing tuning of false positives. The plan is to wire it up *after* the system stabilises, not before.

  **Trigger condition to revisit**: all of (a) Phase B (live IBKR fills) has been running for ~3+ weeks without schema churn on `trade_log` / `signal_runner_log`; (b) ≥10 manual `/daily-run-review` runs have built up a calibrated sense of which checks catch real bugs vs. fire on benign variation; (c) daily attention starts drifting away from the trading app to other work. Don't ship before all three — the manual review is cheaper than maintaining a noisy automated one.

  **Implementation when ready** (~half-day once triggered):
  1. **Two-tier design** — cheap parallel *investigation-only* `Agent` calls every run (grep + anomaly detection, no test-writing, no worktrees), and a heavier *fix-and-verify* worktree pass only when investigation flags something. Avoids paying full cost on clean days.
  2. **Trigger mechanism** (pick one): (a) `/schedule` routine firing ~30 min after the daily window; (b) append `claude -p "<review prompt>"` to the tail of `run_daily.bat`; (c) Stop hook in `settings.json` that fires when the daily Claude session ends. Option (b) is simplest if `run_daily.bat` is already invoking Claude; otherwise (a) is the cleanest separation.
  3. **Investigation focus areas** (initial set — refine after manual runs reveal which patterns actually recur): suspicious stop-loss prices vs entry, orphan bracket legs in close-position paths, exit_reason inconsistencies (especially `fold_end` and trail variants), realised vs unrealised P&L reconciliation, WF short-gating violations, trail activation timing bugs.
  4. **Output**: severity-ranked report written to `logs/reviews/review_YYYYMMDD.md` (new directory). Optional Page 11 dashboard surface if the volume warrants it.

  **Why not a hook fired by the harness directly**: the work needs file reads, grep, and (in the fix-and-verify tier) git worktrees + pytest — full Claude tool access, not a shell-only hook. The hook/cron just *invokes* Claude with the right prompt; Claude itself does the parallel `Agent` orchestration.

- **Pervasive SELL bias across universe — investigate model bias** *(partial mitigation code complete 2026-05-11 — awaiting live verification; Option B retrain still pending if A+C insufficient)* (carry-over from 2026-05-04 long-only-gate verification, broadened by 2026-05-11 daily run): the 2026-05-03 weekly retrain produced **zero closed trades** in `trade_log` for BA, CHTR, CRCL, IWM, NFLX (visible because they correctly drop out of the deduped Page 10 view).  Every fold for those 5 symbols had `Sharpe = 0.000` in the weekly run log too — the gate wasn't being exercised at all because the ensemble emitted *only* SELL signals (now no-ops under `allow_short_selling=False`) or never crossed `signal_threshold`.  Two competing hypotheses: (a) those models trained on bearish stretches and now over-emit shorts as a bias artefact, or (b) the test windows for the 2026-05-03 run happened to align with periods where the model would have shorted (regime-specific, not model-bias).  Investigate by: (1) querying `signal_log` for those 5 symbols in the latest run's test windows — are SELL signals dominating, or is `passed_gate=False` because `|ensemble_score| < threshold`?  (2) checking ensemble weights in `ensemble_weight_history` — did they drift toward a heavily-weighted model that mostly outputs negatives?  (3) sample a few `walk_forward_results` rows for those symbols and compare `n_signals` / `win_rate` to the universe average.  If hypothesis (a) holds, options are retraining with rebalanced labels (force 50/50 BUY/SELL distribution), inverting the signal direction at the gate (treat SELL → don't trade, but also flag for human review since model is bearish on a stock), or removing those symbols from the universe.  Hypothesis (b) is benign — wait one more weekly retrain and see if any of the 5 surface long trades.  Defer until Phase B lands so realised P&L data informs the diagnosis instead of WF metrics alone.

  **Update (2026-05-11 daily run — bias is universe-wide, not symbol-specific)**: the 5-symbol observation above understated the problem.  Today's daily run generated 29 signals across 67 universe symbols (POET excluded — see XGBoost `inf` bug below): **28 SELL + 1 BUY** (TMUS) = **96.5% SELL**.  With `allow_short_selling=False` and held positions (AON, ASTS, AZN, SCHW, SNOW, SYY, TEL, TMUS, UAL) not overlapping any of the 28 SELL targets, **zero trades executed**: 28 × REJECTED_NO_POSITION + 1 × REJECTED (TMUS already held) + 0 longs closed.  The system is technically functioning correctly — it correctly refuses to short — but it's been rendered effectively idle by an ensemble that emits SELL ~30× more often than BUY.  This is hypothesis (a) at universe scale.  Market context argues against "it's just a selloff": VIX = 18.29 (not panic), held-position daily Δ = **+0.65%** (the names we own are *up*, so the model's bearish view contradicts the actual price action of the most-recent bars it trained on).  Concrete next-session work: (1) dump `signal_log` BUY-vs-SELL counts per symbol over the last 30 days to confirm the ratio holds historically, not just today; (2) check `ensemble_weight_history` for the latest run — has XGBoost dominated and is it the source of the bearish skew?  (3) audit training-label balance per symbol — what fraction of `sign(5-bar forward return)` labels in the WF train windows are +1 vs -1?  If train labels are roughly balanced but predictions skew strongly negative, the model has learned a directional bias that survives class-balanced data — likely a feature/target alignment issue.  (4) consider retraining with `scale_pos_weight` on XGBoost or class weights on LSTM to enforce balanced predictions.  Elevated from "deferred until Phase B" to "next-session priority" — the system is currently incapable of opening new longs at any meaningful rate, which makes Phase B's whole purpose (capturing realised live P&L) moot.

  **Diagnosis (2026-05-11 same session)**: ran steps (1)-(3) above.
    - **Step 1** (signal mix, last 14 days from `order_decisions`): BUY=21, SELL=253 (~12:1).  Today 1:28.  Confirmed the bias is not a one-day artefact.
    - **Step 2** (component-score distribution from `signal_log`, last 30 days, n=226): LSTM mean **+0.05** (119↑/104↓, balanced).  **XGBoost mean −0.58 (31↑/194↓, 86% negative)**.  FinBERT mean +0.01.  Weighted with the configured baseline weights (~0.43 / 0.38 / 0.19): `0.43×0.05 + 0.38×(−0.58) + 0.19×0.01 ≈ −0.20`, matching the observed ensemble mean of **−0.18**.  **XGBoost is the sole driver** of the bias; ensemble weights are not structurally skewed.
    - **Step 3** (training-label balance): mean `P(close↑ in 5 bars) = 0.538` across today's 29 universe symbols, std 0.04, only 2/29 below 0.45.  Labels are slightly *bullish* — **hypothesis (a) "label imbalance" is falsified**.
    - **Root cause** (revised, not in the original hypothesis list): XGBoost has correctly learned the empirical pattern "high RSI + price above upper BB → 5-bar mean reversion".  Current bars across the universe sit at the top quartile of own-history RSI (AAPL 0.74, MSFT 0.82, AMZN 0.94, GOOGL 0.87, META 0.83) with prices above the upper Bollinger Band and 5-13% above EMA50.  In a sustained rally, this rule fires constantly but 5-bar mean reversion doesn't materialise.  The model is being *too good* at short-term mean-reversion in a trend-dominated regime.  This is hypothesis (b) in the original entry with a specific mechanism, not hypothesis (a).
    - **Compounding factor**: 132/226 recent bars were classified TRENDING, where `_adjusted_threshold` multiplied the base threshold by **0.9** — making the gate *more* permissive in exactly the regime where XGBoost is most wrong.  This was a sign-flipped design choice (intent: "trends persist, accept weaker signals" works for LSTM; backfires when XGBoost is the dominant emitter of bearish signals against the trend).

  **Status (Option A + C — 2026-05-11):** implemented.
  - **What was implemented:**
      * `config/settings.py`: three new `MLConfig` knobs — `xgb_trending_weight_multiplier: float = 0.5`, `trending_threshold_multiplier: float = 1.2` (was hardcoded 0.9), `high_vol_threshold_multiplier: float = 1.5` (was hardcoded — now configurable too for symmetry).
      * `models/ensemble.py`: `EnsembleModel.__init__` now instantiates a `RegimeDetector`; `predict()` accepts an optional `regime` kwarg (falls back to internal detection), halves XGBoost's effective weight in TRENDING, and redistributes the shed weight proportionally to LSTM and FinBERT based on their relative non-XGB weights.  Returns the detected/passed regime in the scores dict under key `"regime"` so downstream consumers (signal_gate, walk-forward) can reuse it without re-detecting.
      * `models/signal_gate.py`: `evaluate()` now reads `scores["regime"]` and only calls `RegimeDetector.detect()` as a fallback — eliminates the duplicate detection that previously happened every signal.  `_adjusted_threshold` pulls multipliers from `config.ml` instead of hardcoded constants.
  - **Test coverage added (5 new in `tests/test_models.py`):**
      * `test_trending_raises_threshold` — pins the 1.2× multiplier (an ensemble of 0.40 passes base 0.35 but fails 0.42).
      * `test_signal_gate_reuses_regime_from_scores` — asserts `RegimeDetector.detect` is NOT called when `scores["regime"]` is present.
      * `TestEnsemblePredictRegimeAdjust::test_trending_halves_xgb_contribution` — synthetic ensemble (LSTM=+0.5, XGB=−1.0, FinBERT=+0.5) flips from −0.025 (MEAN_REVERTING) to +0.24 (TRENDING with halving), proving the math directionally unbiases the score.
      * `test_mean_reverting_leaves_weights_alone` — same component scores under MEAN_REVERTING produce the baseline-weighted result (XGBoost is appropriate there).
      * `test_caller_can_pass_regime_to_skip_detection` — pinning the regime-passthrough contract.
      * Full suite: **180/180 passing** (was 175 before this work).
  - **Simulation against 30 days of real `signal_log` data**: re-scoring with the new weights and gate thresholds (skipping filter 3 for simplicity) shifts the BUY/SELL/HOLD mix from **9/74/143 → 30/78/118**.  TRENDING-regime mean ensemble score moves from **−0.25 → −0.14** (~45% less bearish).  BUY count roughly triples even though TRENDING is now stricter (0.9× → 1.2×) — the win comes from XGBoost no longer dragging the ensemble score deep into bearish territory.
  - **Why A + C together, not A alone**: A (halve XGB in TRENDING) shifts the score distribution, but with the original 0.9× threshold many of the now-less-bearish scores would still pass the gate as SELL by sliding through the laxer threshold.  C (1.2× threshold) catches those marginal cases — only the strongest signals get through, and they're disproportionately the ones where LSTM/FinBERT actively agreed.  Pair confirmed by simulation: A alone would have produced ~22 BUY / ~90 SELL; A+C produces ~30 BUY / ~78 SELL.
  - **Verification pending:** next daily run (Mon-Fri 09:35).  Expected first-day signature in `order_decisions`: TRENDING-classified symbols show a meaningfully wider BUY:SELL ratio than the recent 1:12.  Over 3-5 daily runs, expect the universe-wide ratio to land closer to 1:3 — still SELL-heavy because the underlying market is overbought, but no longer pathological.  If the ratio remains >5:1 SELL after 5 daily runs, escalate to **Option B (lengthen XGBoost's `_FORWARD_BARS` from 5 to ~20 + Sunday `--force` retrain)** — a 20-bar horizon catches trend continuation instead of short-term reversal, which addresses the root design choice rather than papering over it.  The Option B trigger condition is "A+C insufficient after 1 trading week of observation"; defer Option B until then to avoid stacking changes before measuring A+C's effect in isolation.
  - **Calibration knobs surfaced for future tuning** (all in `config.ml` so YAML-settable from Page 5): `xgb_trending_weight_multiplier` (0.0 = drop XGB entirely in TRENDING, 1.0 = no adjustment, current 0.5), `trending_threshold_multiplier` (1.0 = no penalty, current 1.2), `high_vol_threshold_multiplier` (current 1.5, unchanged from the historical hardcoded value).  If A+C's first week shows over-correction (too few signals overall), lower `trending_threshold_multiplier` toward 1.0 before touching the XGB knob.

  **Update (2026-05-13 — priority downgraded from "next-session" back to "observe; defer to Phase B")**: after the 5/13 daily-run review the user pushed back on the framing of this as a bug — and the pushback is correct.  The two prior elevations (5/11 "next-session priority" because the system was "incapable of opening new longs at any meaningful rate") and the 5/13 daily-review HIGH severity flag both assumed *the model is broken because it doesn't BUY enough*.  But the 5/11 root-cause diagnosis itself contradicted that assumption: XGBoost is correctly learning the empirical "high RSI + price above upper BB → 5-bar mean reversion" pattern, and the 5/13 universe-wide context (VIX=18.28, RSIs at top quartile of own-history, names that triggered SELL are *already in pullbacks* with deep negative MACD like CHTR/LHX/HCA, account up +0.35% intraday on held positions) is consistent with the model being directionally correct in an overbought tape rather than misfiring.  A model that finds longs every day regardless of regime is a *worse* model, not a better one.  The "system is idle" framing is also belied by the daily P&L on held positions — being idle on new entries while existing winners run is fine.  **Concrete priority changes**: (a) drop the HIGH severity flag from the daily-review punch list; this is "observe, don't act"; (b) do **not** pull the Option B trigger ("ratio remains >5:1 SELL after 5 daily runs") on the BUY-count metric alone — that metric measures the symptom, not the disease; (c) Option B's real trigger condition is **realised-P&L evidence**, which means waiting for Phase B (live IBKR fills via `reqExecutions` polling) to accumulate enough `source='live'` rows to compare against the model's rejected-SELL set — if XGBoost says SELL on names we don't hold and those names *do* drop over the next 5-20 bars, the model is signal not noise, and "fixing" the BUY rate would destroy that edge; if they're flat or up, then Option B (lengthen `_FORWARD_BARS` 5→20) becomes the right intervention because the model is overconfident on near-term reversal; (d) the BUY-count trajectory (1:28 → 2:17 → 3:19 across 5/11→5/12→5/13) is going the right direction post Option A+C and that's enough for now.  **Defer Option B until ≥2 weeks of Phase B `source='live'` rows exist** (the same realised-P&L data Phase C consumes), and use that data — not WF Sharpe and not BUY:SELL ratios — to decide whether the bias is alpha or artefact.  Pinning this note so the next agent who reads the daily-review punch list doesn't re-elevate this issue on count-based grounds.

- **Raw Kelly f* values below -1 logged without clamping note** *(code complete 2026-05-12 — awaiting live verification)* (low priority, observed 2026-05-11 daily run, `models/walk_forward.py` Phase C diagnostic): the per-fold `realised-Kelly history` log line prints the raw computed f* from `compute_realised_kelly`, which can produce values < -1 when the realised history is loss-heavy (e.g. TSCO Fold 2: `f*=-1.087`; TSCO Fold 5: `f*=-0.777`; AEM-class symbols hit `f*=-2.177` in the same run).  The downstream `PositionSizer._kelly_fraction` correctly floors negative f* to 0 (verified by `test_realised_kelly_negative_fstar_floors_to_zero` — no actual sizing bug), but the raw log value is misleading without context: an unfamiliar reader sees `f*=-2.177` and might think the system is about to short more than 2× capital.  Two cheap fixes: (a) append `(→ 0, would short)` to the log line when `f* < 0`, so the audit trail makes the floor explicit, or (b) cap the printed value at `-1.0` and append a `*` footnote indicator.  Option (a) is more informative and reads more naturally in `grep` output.  ~5-line change in the Phase C diagnostic log site.  Defer until the next time someone is grepping these logs and gets confused — low impact, easy to spot when it matters.  **Status:** implemented as `_format_kelly_fstar` static helper on `MLWalkForwardOrchestrator` (`models/walk_forward.py:67-84`) — handles three cases: `None` → `"n/a"`, negative → `"-X.XXX (→ 0, would short)"`, positive → `"X.XXX"`.  Called from the fold-start diagnostic log site at line 151.  Verification pending: next Sunday `--force` weekly retrain — any symbol with loss-heavy realised history (TSCO / AEM-class were the 2026-05-11 examples) should now show the explicit clamp annotation in `logs/weekly/*.log`.

- **Page 10 symbol filter dropdown still lists symbols absent from the deduped view** *(code complete 2026-05-12 — awaiting live verification)* (UX nit, low priority): `query_trade_log_filter_options` populates the sidebar Symbol multiselect from the *raw* `trade_log` (`get_trade_log()` no filters), so symbols whose latest WF run produced zero closed trades (currently BA, CHTR, CRCL, IWM, NFLX) are still selectable in the dropdown but yield an empty table when picked while dedup is on.  Two options: (a) make `query_trade_log_filter_options` accept the same `dedup_to_latest_run` flag and source from the deduped view, or (b) annotate stale-only symbols in the dropdown with a `(no current trades)` suffix so they're visible but de-emphasised.  Option (b) is more informative — preserves the user's ability to drill into stale history by toggling dedup off — but slightly more work.  Defer until the user actually hits the confusion in practice.  **Status:** went with option (a) — `query_trade_log_filter_options` now accepts a `dedup_to_latest_run: bool = True` parameter and, when true, runs the same `_keep_latest_run_per_symbol` helper used by `query_trade_log` before extracting the unique symbols / exit_reasons / sources.  Toggling dedup off in the sidebar brings the full list back.  The Page 10 dedup checkbox is wired to a new session_state key (`trade_history_dedup`) so the filter-options query at the top of the script picks up the previous run's checkbox value (Streamlit reruns the script top-to-bottom on every interaction).  Help text on the checkbox now mentions that it also filters the dropdowns above.  No new unit tests — the change is a one-line dedup gate over an existing helper that's already covered by `query_trade_log`'s dedup tests; UX behaviour is verified by inspection.  Verification pending: load Page 10 with dedup ON and confirm BA / CHTR / CRCL / IWM / NFLX (the 2026-05-04 zero-trade symbols) no longer appear in the Symbols multiselect; toggle dedup OFF and confirm they reappear.

- **Track IBKR / Alpaca / yfinance news source hit-rate over time** (observability nice-to-have): the 2026-05-03 weekly run showed 38 of 68 symbols (~56 %) timing out on `reqHistoricalNewsAsync` and falling back to Alpaca / yfinance.  The 3-tier fallback caught all of them, so it's not a correctness bug — but the 56 % rate is high enough to be worth watching, and a slow upward drift would degrade FinBERT coverage silently before any signal-quality alarm fires.  Implementation: parse the `Fetched N articles for SYM from {IBKR|Alpaca|yfinance}` log lines or instrument `NewsClient.fetch_news` to write a per-fetch row to a new `news_fetch_log` table (run_id, symbol, source, n_articles, duration_ms, error).  Surface on Page 6 (Data Status) as a "Source mix" donut + a "IBKR timeout rate, last 7 daily runs" sparkline.  Defer until either (a) the timeout rate visibly creeps above ~70 %, or (b) we hit a stretch where IBKR is producing *zero* news for >half the universe (that's the failure mode that would actually starve FinBERT).

- **Adopt IBC (IB Controller) for unattended IB Gateway operation**: IB Gateway silently logs out overnight (observed: user finds it logged out most mornings and must re-launch + re-enter credentials manually), which breaks `run_daily.bat` Phase 4 / Phase 3.5 whenever the morning task runs before the manual restart. [IBC](https://github.com/IbcAlpha/IBC) is the standard open-source wrapper: launches IB Gateway on boot, enters paper/live credentials from a config file, handles the daily 24h session reset, and auto-restarts on unexpected disconnect. Setup is a self-contained install (no code changes in this repo) — just point it at the existing `IB Gateway 10.x` install and wire it into Windows Task Scheduler in place of launching IB Gateway manually. Until this is in place, morning runs can silently fall back to dry-run (`⚠ IBKR unreachable — falling back to dry-run for this phase`) even though `paper_orders_enabled=True`. If IBC still proves flaky, the fallback plan is migration to Alpaca (pure REST API, no desktop app) — larger effort: rewrite `execution/ibkr_connection.py`, demote the IBKR news tier, rework `risk/trailing_stop.py` to Alpaca's `trail_price` / `trail_percent` order params, and replace the Page 9 IBKR account view. Alpaca supports bracket orders and native trailing stops so the risk-layer surface area stays similar.

  **Scope correction (2026-05-07)**: this enhancement is **no longer a Phase B prerequisite**. Phase B's design pivoted from a live `execDetails` subscription (which would have required Gateway uptime continuity to avoid missing fills) to polling reconciliation via `reqExecutions` at the start of each daily run — that approach tolerates Gateway downtime by design, since IBKR retains 7 days of execution history server-side regardless of connection state. IBC remains valuable for *Phase 4 live-order submission* (a Gateway-down morning still means brackets aren't placed when signals fire, which is a missed-trade cost not a missed-fill cost), but no longer gates Phase B work. Updated priority: nice-to-have for trade execution timeliness; not blocking any current roadmap item.

- **Phase 4.5 — Realised P&L plumbing (brackets in WF + `trade_log` + realised-Kelly)** *(Phase A verified live 2026-05-04 — long-only gate confirmed; Phase C implemented 2026-05-07 — Sunday-retrain verification pending; **Phase B SHIPPED 2026-05-29 — pipeline verified live, moved to CHANGELOG.md (2026-05-29 section); end-to-end paired round-trip verification deferred to the first COHR/MRVL/WDC/USO exit, tracked in `docs/reviews/followups.md`**)*: Bundles four previously-separate items that share a single keystone — the `trade_log` table:
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

  **Phase B — live fill reconciliation** *(SHIPPED 2026-05-29 — see CHANGELOG.md 2026-05-29 section for the as-built summary + live-verification caveats; the design text below is retained as the spec it was built against)* (design pivoted 2026-05-07 from live subscription to polling reconciliation):

  **Design rationale**: bracket orders are GTC, so fills happen *between* daily runs (e.g. TP filling at 14:42 Tuesday while signal_runner only runs at 09:35). A live `execDetails` subscription would miss every such fill unless IB Gateway stayed up continuously and signal_runner stayed connected — exactly the operating environment we *don't* have today (Gateway logs out overnight, signal_runner exits after each daily run). IBKR retains 7 days of execution history server-side via `reqExecutions`, so a polling reconciliation at the start of each daily run is **strictly more robust than a live subscription**: tolerates Gateway outages, tolerates skipped runs, idempotent on rerun, and drops the IBC-uptime prerequisite entirely. Only failure mode is signal_runner not running for >7 consecutive days, recoverable via IBKR Flex Query reports (manual one-off).

  **Schema additions** (additive `_migrate()` ALTERs + two new tables):

  1. **New `fill_log` table** — raw IBKR executions, idempotent on `exec_id`. One row per IBKR `Execution` object; the audit trail / source of truth from which `trade_log.live` rows are aggregated.
     ```
     id              INTEGER PK
     exec_id         VARCHAR(40) NOT NULL UNIQUE   -- IBKR's stable execution ID
     order_id        INTEGER                        -- IBKR order ID (entry leg or child)
     parent_order_id INTEGER                        -- bracket parent (NULL for entries)
     account         VARCHAR(20)                    -- IBKR account code
     symbol          VARCHAR(10) NOT NULL
     side            VARCHAR(4)  NOT NULL           -- 'BUY' | 'SELL'
     order_type      VARCHAR(10)                    -- 'LMT' | 'STP' | 'STP LMT' | 'TRAIL' | 'MKT'
     shares          FLOAT       NOT NULL
     price           FLOAT       NOT NULL           -- avg fill price for this exec
     commission      FLOAT                          -- from commissionReport (may arrive separately)
     realized_pnl    FLOAT                          -- IBKR-reported per-fill realised P&L
     exec_time       DATETIME    NOT NULL           -- IBKR fill timestamp (UTC-naive)
     recorded_at     DATETIME    NOT NULL
     ```
     `UNIQUE(exec_id)` enforces idempotency: re-running reconciliation on overlapping windows can't double-write.

  2. **New `reconciliation_state` table** — single row per source/account tracking the reconciliation watermark.
     ```
     id                  INTEGER PK
     source              VARCHAR(20) NOT NULL     -- 'live' (future: 'live_subaccount_X')
     account             VARCHAR(20)              -- IBKR account code (None for current single-account setup)
     last_reconciled_ts  DATETIME                 -- newest exec_time we've persisted
     last_run_ts         DATETIME                 -- when reconciliation last ran
     last_n_fills        INTEGER                  -- how many fills last run picked up
     notes               TEXT                     -- e.g. 'reqExecutions returned 0 — empty window'
     UNIQUE(source, account)
     ```
     `last_reconciled_ts` is what bounds the next `reqExecutions` call (via `ExecutionFilter.time`). If null (first run), default to "now − 7 days" — the IBKR retention horizon.

  3. **`trade_log` additions** — link aggregated trades back to their fills.
     ```
     entry_exec_id   VARCHAR(40)   -- the entry-leg fill that opened the position
     exit_exec_id    VARCHAR(40)   -- the exit-leg fill that closed it
     parent_order_id INTEGER       -- bracket parent for cross-reference
     account         VARCHAR(20)   -- per-account scoping (None for current setup)
     ```
     Existing `walk_forward` rows leave these as NULL; only `live` rows populate them. Composite `UNIQUE(source, entry_exec_id, exit_exec_id)` would be ideal but SQLite handles NULLs as distinct, so a partial unique index `WHERE source='live'` is the SQLite-friendly form (or just an application-level dedup check during reconciliation).

  **Reconciliation flow** (Phase B implementation, runs once per `signal_runner.py` Phase 1):

  1. Read `reconciliation_state.last_reconciled_ts` for `(source='live', account=current)`. Default to `now − 7 days` if first run.
  2. Open IBKR connection (skip phase if unavailable — log warning, no state mutation).
  3. Call `ib.reqExecutionsAsync(ExecutionFilter(time=last_reconciled_ts))` → list of `Fill` objects.
  4. **Pass 1 — `fill_log` ingestion**: for each `Fill`, INSERT OR IGNORE on `exec_id`. Log how many were new vs. skipped (skipped = idempotency working).
  5. **Pass 2 — `trade_log` aggregation**: pair entry fills with exit fills via `parent_order_id`. When a position has gone net-flat (sum of entry-side shares == sum of exit-side shares), write one `trade_log` row per round trip:
     - `entry_px` = volume-weighted avg of entry fills
     - `exit_px`  = volume-weighted avg of exit fills
     - `shares`   = filled quantity
     - `pnl`      = sum of realised dollar P&L from IBKR (already net of commission per-fill — preserves the `pnl is net` schema convention from Phase A)
     - `costs_charged` = sum of commissions across all fills
     - `exit_reason` derived from exit-leg `order_type`: `LMT` → `tp`, `STP`/`STP LMT` → `stop`, `TRAIL` → `trailing`, `MKT` → `manual_close`. (Signal-flip and fold-end never apply to live trades — those are WF-only concepts.)
  6. Update `reconciliation_state.last_reconciled_ts` to `max(exec_time)` from this batch (so next run starts where this one ended).
  7. Trades partially open at end-of-window stay in `fill_log` only — they're picked up on a future reconciliation once the position closes.

  **Open design questions** (resolve before code):
  - **Stop-modification fills** (e.g. trailing-stop `auxPrice` updates) — these don't generate `Execution` events, only the eventual fill does. Confirmed safe but worth checking on first live test.
  - **Cancellation events** — bracket children that are cancelled (e.g. via `_cancel_bracket_children` in long-only close path) don't appear in `reqExecutions`. Need to verify our exit-reason inference doesn't get confused by an entry+market_close pair (no STP/LMT/TRAIL fill exists).
  - **Multi-account future-proofing** — the schema includes `account` but we currently only use one IBKR paper account. Cost is one extra column; defer the multi-account *behaviour* (filtering / display) until a real second account exists.
  - **Realised-P&L sign convention** — IBKR's `realizedPNL` field on `Execution` is per-fill and only populated on closing fills. Need to confirm signs match our `pnl_pct = gross_pct − total_costs` convention; may need to invert for shorts. Test against the first real fill before relying on it.

  **Acceptance gates** (before shipping Phase B):
  1. Two clean Sunday `--force` weekly retrains in a row with Phase C diagnostic firing as expected (confirms `trade_log` schema is stable; earliest 2026-05-17).
  2. Five consecutive daily runs with no new bug surfaced by post-run audits.
  3. Phase A's `pnl is net` semantic verified live (already done 2026-05-07 via the SPY accounting fix; pinned by `test_net_pnl_equals_stored_pnl`).

  **Day-1 spot-check** (must pass before second Phase B daily run):
  - **`fill_log` ↔ IBKR reconciliation**: after the first reconciliation run that ingests real fills, manually compare against IBKR's *Trades* view (Account Portal or IB Gateway) for the same day:
    * Row count: `SELECT COUNT(*) FROM fill_log WHERE DATE(exec_time) = '<today>'` should match IBKR's filled-trade count.
    * Sum of shares: `SELECT SUM(shares) FROM fill_log WHERE ...` should match the sum in IBKR.
    * Sum of commissions: `SELECT SUM(commission) FROM fill_log WHERE ...` should match the IBKR Activity Statement.
  - Mismatches indicate a missed `commissionReport` event, an `ExecutionFilter.time` boundary off-by-one, or `account` filter dropping rows. Diagnose and fix before relying on `trade_log.live` rows for realised-Kelly.
  - **`trade_log.live` ↔ `fill_log` aggregation**: for one round-trip trade, manually check `trade_log.shares == fill_log.shares` for the entry leg, `trade_log.entry_px ≈ volume-weighted avg of entry fills`, and `trade_log.pnl + trade_log.costs_charged == sum of IBKR realisedPNL across exit fills`. This validates Pass 2 (round-trip aggregation) before trusting it for Kelly recompute.

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
  - **Verification update (2026-05-04, against 2026-05-03 weekly `--force` retrain):** long-only gate **verified live**.  The Page 10 trade-history table (with the WF-results-driven dedup landed in the same session — see the Page 10 dedup architectural decision below) shows **0 SELL rows** across all 68 universe symbols' latest training runs.  All 39 SELL rows that initially appeared on Page 10 had `recorded_at` between 17:33–18:12 on 2026-04-30 (the pre-fix run), and survived only because the dedup originally fell back to trade_log when the post-fix retrain produced zero closed trades for 5 symbols (BA, CHTR, CRCL, IWM, NFLX) — switching the dedup to source from `walk_forward_results` correctly drops them.  Other observable signals: (1) per-symbol row count for the latest run averages ~4 trades/symbol vs the pre-fix ~8; (3) WF Sharpe distribution still shows a long left tail (worst -2.21 MCK, best +2.17 XOM) — confirming the prior shorts-induced noise wasn't the *only* source of negative-Sharpe folds; long-only alpha genuinely needs more work for tech / discretionary names (XLY -2.14, MCK -2.21, PLTR -2.04, XLE -1.88).  See "5 symbols with zero current-model long trades" enhancement below.  Phase B is still pending; Phase 4.5 as a whole stays "in progress" until it lands.

  **Status (Phase C — 2026-05-07):** implemented (live verification pending).
  - **What was implemented:**
      * `risk/position_sizer.py`: new top-level `compute_realised_kelly(symbol, as_of=None, lookback_n=100, source=None, run_id=None)` helper.  Reads `trade_log`, returns `{n_trades, win_rate, avg_win_pct, avg_loss_pct, b, f_star}` from the most recent matching trades — or `None` when nothing matches.  Forward-only safety via `entry_ts < as_of`.  All-wins / all-losses windows return `b=None, f_star=None` so the caller falls back rather than dividing by zero.
      * `PositionSizer.calculate` gains `kelly_history: dict | None = None` kwarg.  Priority chain in `_kelly_fraction`: realised history (`method='kelly_realised'`) → signal_log proxy (`method='kelly_proxy'` — was `'kelly'`) → fixed.  Realised path engages only when `n_trades >= RiskConfig.min_trades_for_realised_kelly` (default 30) AND `f_star is not None`; otherwise the legacy proxy/fixed path runs.  **Method-label rename**: `'kelly' → 'kelly_proxy'` everywhere.  The corresponding `tests/test_risk.py::TestPositionSizer::test_kelly_calculation_with_history` assertion was updated.
      * `models/walk_forward.py`: orchestrator computes `kelly_history` once at the start of each fold (`as_of=fold.test_start, source='walk_forward', run_id=self._run_id`) — naturally excludes the current fold's trades and trades from prior runs with different ensemble weights.  Threaded into `_run_test_window` via a new `kelly_history` kwarg.  `_sizer = PositionSizer()` instantiated at orchestrator construction; entry block calls `self._sizer.calculate(..., kelly_history=kelly_history)` and stores `trade_shares` for the active position.  `_close_trade` now writes Kelly-sized `shares`, dollar `pnl = pnl_pct × entry_px × shares`, and dollar `costs_charged = total_costs × entry_px × shares` to `trade_log`.  When the sizer returns `shares < 1` (e.g. f* ≤ 0 from a lose-heavy realised history, or notional is too small at the current price), the entry is skipped — mirrors `OrderManager.REJECTED_TOO_SMALL`; bar_pnl stays 0 and no trade is logged.
      * `risk/order_manager.py`: `OrderManager.process` calls `compute_realised_kelly(symbol=symbol, source='live')` once per signal and passes the result into `PositionSizer.calculate`.  Until Phase B starts populating `source='live'` rows, the helper returns `None` and the proxy path runs unchanged — so no behavioural change today, but Kelly sizing will start tracking realised broker fills the moment the live-fill subscription lands.
  - **Design choice — bar_pnl stays size-agnostic.**  Per-bar P&L (and therefore `walk_forward_results.sharpe_ratio` / `total_return` / `max_drawdown`) is unchanged: scaling every bar's contribution by a fold-constant `position_pct` is a no-op for Sharpe, and the per-trade pnl_pct in `trade_log` remains the natural Kelly input.  Only `shares`, `pnl`, and `costs_charged` in `trade_log` (and Page 10 sums derived from them) reflect the Kelly-sized position.  This keeps the WF Sharpe interpretation consistent across pre- and post-Phase-C runs.
  - **Test coverage added (19 new):**
      * `tests/test_risk.py::TestPositionSizer` — 4 new: `test_realised_kelly_history_used_when_threshold_met`, `test_realised_kelly_below_threshold_falls_back_to_proxy`, `test_realised_kelly_undefined_falls_back`, `test_realised_kelly_negative_fstar_floors_to_zero`.
      * `tests/test_risk.py::TestComputeRealisedKelly` — 6 new: empty / basic stats / forward-only / run_id filter / source filter / all-wins-undefined.
      * `tests/test_walk_forward.py::TestBracketSimulation` — 2 new: `test_realised_kelly_history_drives_trade_shares` (asserts `trades[0]['shares']` matches what `PositionSizer.calculate` returns for the same `kelly_history`), `test_zero_share_kelly_skips_entry` (sizer returns 0 shares → no trade logged, returns all 0).  Existing `test_trade_log_record_fields_populated` updated: `shares >= 1` (was `== 1.0`), `pnl == pnl_pct × entry_px × shares` (was `× 1`).
      * Full suite: **171/171 passing** (after the 2026-05-07 Page 10 P&L-accounting fix below adds 6 more).
  - **Verification pending:** the next Sunday `run_weekly.bat --force` retrain is the natural test case.  Symbols with ≥30 closed `walk_forward` trades by the start of fold 2 should show `method='kelly_realised'` in the new fold-start log line (`Fold N: realised-Kelly history n=… win_rate=… f*=…`); cold symbols stay on the proxy path.  Page 10 trade-history rows for the new run will carry Kelly-sized `shares` (currently always 1) and dollar `pnl` proportional to those shares — straightforward to spot-check by sorting on the new shares column.  Live `source='live'` Kelly is gated on Phase B and won't activate until broker fills start writing rows.

- **Trade History dashboard page + tax/net-profit analytics** *(verified live 2026-05-04 against 2026-05-03 weekly retrain; dedup-by-WF-results refinement landed same day — see status update at end of this entry)* (new dashboard surface over `trade_log`): build a `dashboard/pages/10_Trade_History.py` page that turns the existing `trade_log` table into a human-readable record of closed trades, with realised P&L net of `costs_charged`, holding-period classification (short-term vs long-term), and an indicative tax-impact view. Phase 4.5 Phase A already populates the table with WF-simulated rows, so the page has data on day one; Phase B (live IBKR fills) will add `source='live'` rows to the same table later, and the page's `source` filter is what surfaces them.

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
  - **Verification update (2026-05-04):** **verified live.**  The 2026-05-03 weekly retrain populated `trade_log`; Page 10 rendered cleanly, summary cards / cumulative-P&L / exit-reason donut all working, and the per-symbol breakdown surfaces realistic trade counts and P&L.  Phase B (live IBKR fills) → adds `source='live'` rows; the existing source filter toggle surfaces them with no code change.

  - **Status (2026-05-04 dedup refinement):** during verification, the page initially surfaced 39 stale SELL rows from the pre-2026-04-30 training run because each weekly `--force` retrain bulk-inserts a new `run_id` without truncating prior rows.  Two-part fix landed the same session:
      * **`data/ui_queries.py`**: new `_keep_latest_run_per_symbol` helper + `dedup_to_latest_run: bool = True` parameter on `query_trade_log` / `query_trade_summary` / `query_tax_breakdown`.  The helper sources "latest run_id per symbol" from `walk_forward_results` (one row per fold, written every retrain regardless of trade count) **rather than from `trade_log` itself** — see "Page 10 dedup sources truth from walk_forward_results, not trade_log" architectural decision for the rationale.  Live (`source='live'`) rows always pass through untouched.  Specific `run_id` filter short-circuits the dedup automatically.
      * **`dashboard/pages/10_Trade_History.py`**: new sidebar checkbox **"Dedupe to latest run per symbol"** (default ON) wired into `filter_kwargs`; new **Recorded At** column added to the trades table and CSV export.
  - **Verification of the dedup refinement** (same session): on the production DB, dedup reduced 1087 → 266 rows (75 % drop) with 0 SELL rows surviving — long-only gate confirmed.  146/146 tests still pass.

### Design notes (not bugs)

**FinBERT `evaluate()` is a stub**: Returns `{"total_return": 0.0, "sharpe_ratio": 0.0}` always. This is intentional (sentiment can't be evaluated like a price model) but it means FinBERT never wins the LSTM/XGBoost competition — hence the coverage-based weighting as a substitute quality signal.

**Walk-forward cost model is approximate**: `slippage_pct` and `commission_per_share` are applied as flat adjustments. No market impact, no partial fills, no bid-ask spread model. Sufficient for learning purposes.

**No position sizing in walk-forward**: The signal gate outputs BUY/SELL/HOLD but the walk-forward P&L assumes 1 unit per signal. The `risk/position_sizer.py` module (Phase 4) provides Kelly/ATR sizing for live trading via `signal_runner.py`, but is not wired into the walk-forward backtester — integrating it there would require forward-only sizing (no future data in Kelly history) to avoid lookahead bias.

**0-day `fold_end` trades on Page 10 are real, not artefacts** (`models/walk_forward.py:_run_test_window`): roughly 6 % of WF trades show `entry_ts == exit_ts` with `exit_reason='fold_end'` (e.g. MDT BUY 2026-05-01→2026-05-01 +$0.43 net, AXON BUY 2026-04-01→2026-04-01 -$10.70 net, LULU BUY 2026-04-01→2026-04-01 +$7.26 net).  These look anomalous but are by design: the simulator opens at bar `t` open after a signal at bar `t-1` close, and if `t` is the last bar of the test window the fold_end force-flatten fires at bar `t` close — producing a legitimate single-bar intra-day trade with non-zero P&L.  They cluster at fold boundaries (one per fold per still-open position) rather than spreading randomly across the test window.  Two consequences for Page 10 stats: (a) the per-symbol "Avg Days" metric is biased downward by the 0-day cluster — typically by ~10-15 % depending on how often a symbol enters near a fold boundary; (b) the exit-reason donut overweights `fold_end` proportionally.  Not a bug, but if Page 10 stats become misleading enough, options are: (i) bucket 0-day fold_end into a separate "fold_end (0-day)" donut wedge so the larger fold_end count is visible separately, (ii) exclude `holding_days == 0 AND exit_reason == 'fold_end'` from the per-symbol Avg Days calc only (still count them in P&L), or (iii) lower priority — leave alone.

**Intraday runner inherits daily-runner IBKR connect parameters** (`config/settings.py:IBKRConfig.max_reconnect_attempts=5`, `reconnect_delay=5.0`): when IB Gateway is unreachable at an intraday slot, `IBKRConnection.connect()` retries 5 times with linear back-off (5s, 10s, 15s, 20s = 50s between attempts + ~10s per attempt) — total worst case ~60 seconds before returning False and writing the `gateway_down` row.  Measured 60.3s on the 2026-05-20 Gateway-down verification.  Acceptable but suboptimal for the intraday cadence: at the 12:00 ET slot a 60-second wait is invisible operationally; at 15:30 ET (30 minutes before close) it still has comfortable margin.  Eventually the intraday runner may warrant its own connect parameters (e.g. 2 attempts × 5s back-off = ~10 seconds max) so a flaky-Gateway day burns less wall-clock time across two slots.  **Trigger to revisit**: after ≥4 weeks of operating the intraday runner, if `intraday_run_log` rows with `status='gateway_down'` are common enough (say >5/week) that the 60-second wait becomes operationally annoying.  Until then, the shared defaults match the daily runner's connect behaviour exactly, which is the right epistemic stance — change one thing at a time and only on evidence.

**VIX cache behavior**: `RegimeDetector._get_vix()` serves the cached SQLite value when it is younger than 4 hours. When the cache is stale and the code is running inside a Streamlit session (`streamlit.runtime.exists()` returns `True`), the live yfinance fetch is **skipped** to avoid blocking the UI thread — the stale value is used with a log warning. The VIX cache is refreshed by running `python scripts/run_pipeline.py` (^VIX is always fetched at the start of the pipeline run). The Page 3 sidebar shows the current cached VIX value and its age.

**Streamlit file watcher suppressed**: `fileWatcherType = "none"` in `.streamlit/config.toml` suppresses torchvision import noise in logs. If the file watcher is needed for development, remove that setting.

## Testing Conventions

- Always run the full test suite after schema changes, bracket logic changes, or close-position logic changes.
- Report test count (e.g., '143/143 passing') in completion summaries.
- When writing tests for time-based logic (trailing stops, TP/SL ordering), verify bar values produce the intended event sequence before asserting outcomes.

## Testing Approach

Unit tests mock `ib_insync`, `yfinance`, and all database calls — no live connections or network needed. Run with `.venv/Scripts/pytest tests/ -v`.

- `test_data_pipeline.py`: patches `yfinance.Ticker` and `data.database` functions
- `test_ibkr_connection.py`: patches `ib_insync.IB`
- `test_walk_forward.py`: 23 tests covering WalkForwardSplit edge cases, compute_metrics, orchestrator integration
- `test_models.py`: 14 tests; patches `models.finbert_model.NewsClient` (not AlpacaClient — that module was deleted)
- `test_universe.py`: 15 tests; patches `alpaca.trading.client.TradingClient`, `data.fundamentals.FundamentalsClient`, `data.universe.get_bars`; uses `tmp_path` in-memory SQLite for DB roundtrip tests
- `test_risk.py`: 18 tests; uses in-memory SQLite (`mem_engine` monkeypatch fixture); patches `PortfolioGuard.check` and `OrderManager._get_latest_close` where needed; no IBKR or network required
- `test_signal_runner.py`: 6 tests; patches `signal_runner.OrderManager`; no live connections, yfinance, or DB needed

Integration tests (`verify_connection.py`) require a paper trading account open in IB Gateway.
`verify_universe.py` requires `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` env vars for Stages 1-3; DB helpers are tested without keys.
`verify_risk.py` requires no external services; uses the live `db/trading.db`.
