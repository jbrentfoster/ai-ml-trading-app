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

### Commit messages (Windows / PowerShell)
- **Do not pass multi-line commit messages with `git commit -m @'...'@` here-strings.** This setup runs Windows PowerShell 5.1, where here-strings passed to a native exe are fragile: embedded double quotes (e.g. `"Outstanding bugs"`) and other tokens routinely break argument parsing, and PowerShell scatters the message body across `git` as bogus pathspecs (`error: pathspec '...' did not match any file(s)`). This has recurred across many sessions.
- **Robust pattern — write the message to a file and use `git commit -F`:** create a temp message file with the Write tool (e.g. `.git/COMMIT_EDITMSG_CC.txt`, which is inside `.git/` so it's never accidentally staged), commit with `git commit -F .git/COMMIT_EDITMSG_CC.txt`, then `Remove-Item` it. This sidesteps all PowerShell quoting entirely and is the expected way to make any commit with a body, special characters, or quotes.
- Single-line trivial commits with `git commit -m "short message"` (no embedded quotes) are fine.

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
├── run_daily.bat            — Mon–Fri pre-market scheduler (09:40 ET): run_pipeline.py →
│                              universe_scheduler.py --rescore-now → train_models.py (skip-existing) →
│                              reconcile_flex.py (Step 3c, Flex backstop) → backfill_benchmark_returns.py
│                              (Step 3b) → signal_runner.py --no-dry-run → backfill_benchmark_returns.py
│                              (Step 4b).  Logs to logs/daily/daily_run_YYYYMMDD.log.  Backfill runs
│                              twice bracketing the runner (Step 4b catches Phase-1-reconciled live exits
│                              that land after Step 3b); both passes idempotent (WHERE …_pct IS NULL).
│                              Step 3c rationale → arch note "Flex Web Service is the durable trade-history
│                              source"; Step 4b rationale → CHANGELOG 2026-06-02.
├── run_weekly.bat           — Sunday scheduler: universe_scheduler.py --run-now → run_pipeline.py →
│                              train_models.py --force → backfill_benchmark_returns.py; logs to
│                              logs/weekly/weekly_run_YYYYMMDD.log.  Universe refresh runs FIRST so
│                              pipeline + training operate on the freshly-rotated active set (reordered
│                              2026-05-20 — brand-new symbols otherwise train with no indicators/news).
├── run_eod.bat              — Mon–Fri post-close scheduler (16:30 ET): refresh_recent_bars.py;
│                              logs to logs/eod/eod_run_YYYYMMDD.log.  Wire via Windows Task Scheduler
│                              separately from run_daily.bat (which fires pre-market at 09:40 ET).
├── run_intraday.bat         — Mon–Fri intraday scheduler (12:00 ET + 15:30 ET): intraday_check.py
│                              --no-dry-run; logs to logs/intraday/intraday_run_YYYYMMDD_HHMM.log (HHMM
│                              so per-day runs don't collide).  Two Task Scheduler entries
│                              (TradingApp\IntradayMidday + IntradayLateAfternoon) share this file.
│                              Ratchet-only by default; opt-in TP→TRAIL via
│                              RiskConfig.intraday_trail_conversion_enabled.
├── run_llm_news.bat         — LLM news analyst (shadow): Step 1 ingest_news_bodies.py (needs Gateway)
│                              → Step 2 score_news_llm.py (needs Ollama, slow ~2h).  Both no-op unless
│                              config.llm.enabled=True.  Logs to logs/llm/llm_news_YYYYMMDD.log.  Daily
│                              since 2026-06-02; kept off the pre-market critical path on purpose.
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
│   ├── signal_runner.py     — Daily automation, 7-phase flow (1, 2, 3, 3.5, 3.6, 4, 5): reconcile
│   │                          off-cycle IBKR fills → refresh data → generate signals → trailing
│   │                          stops → hold-timeout flatten → risk/order decisions.  Phase 1 calls
│   │                          execution/reconciliation.py before the CB/baseline check (non-dry-run).
│   │                          Brackets submitted GTC, tick-rounded to $0.01.  Phase 3.6 opt-in via
│   │                          config.risk.hold_timeout_enabled.  --dry-run (default) / --no-dry-run
│   │                          (live paper orders — needs Gateway + paper_orders_enabled) / --symbol /
│   │                          --schedule.  Phase details → arch-decision notes.
│   ├── intraday_check.py    — Intraday lightweight runner (Phase 1 CB check + Phase 3.5 trail
│   │                          re-eval against live IBKR price); 12:00 + 15:30 ET via run_intraday.bat.
│   │                          Does NOT regenerate signals / refresh data / retrain / rescore /
│   │                          hold-timeout (daily-weekly cadence).  One intraday_run_log row per run;
│   │                          Gateway-down → status='gateway_down' + exit 0 (see arch note "Intraday
│   │                          runner exits 0 on Gateway-down").  --dry-run (default) / --no-dry-run
│   │                          (CB-flatten + opt-in trail-conversion) / --symbol (informational).
│   ├── backfill_benchmark_returns.py — Idempotent backfill of trade_log.benchmark_return_pct (raw
│   │                            SPY return over each trade's holding period; WHERE …_pct IS NULL).
│   │                            Wired into run_weekly/run_daily after training.  Raw-vs-net semantics
│   │                            → arch note "Benchmark-relative tracking".
│   ├── backfill_sectors.py   — One-shot backfill of fundamental_data.sector (raw yfinance GICS) for
│   │                          symbols whose latest snapshot predates the 2026-06-03 sector column;
│   │                          union of traded/decided/held ∩ sector-IS-NULL, skips ETFs/fixtures.
│   │                          Going forward FundamentalsClient captures it on every fetch.
│   │                          --symbols / --dry-run.
│   ├── refresh_recent_bars.py — End-of-day refresh (run_eod.bat): overwrites the last --days
│   │                            (default 5) of OHLCV bars + indicators (overwrite=True) for the union
│   │                            of (active universe, recently-acted via order_decisions ≤14d,
│   │                            recently-exited live via trade_log ≤14d [SNOW gap fix], held IBKR
│   │                            positions) — replacing mid-day partial bars with post-close values.
│   │                            ALSO sweeps unfilled GTC entry orders on NOT-held symbols
│   │                            (_cancel_unfilled_entries → order_decisions 'CANCELLED_UNFILLED').
│   │                            --no-ibkr / --no-cancel; no-op when Gateway down.  See arch note
│   │                            "Unfilled entry orders are swept at EOD".
│   ├── reconcile_fills.py   — Phase B CLI: reconcile IBKR fills → fill_log + trade_log via
│   │                          execution/reconciliation.py (same core as signal_runner Phase 1);
│   │                          --since / --dry-run / --symbol.
│   ├── reconcile_flex.py    — Daily Flex reconciliation (durable backstop for between-run live fills):
│   │                          fetch IBKR Flex Query via Web Service (data/flex_client.py) →
│   │                          parse_flex_trades → SAME reconcile_fills core.  No Gateway (HTTPS); T+1.
│   │                          No-op/graceful exit 0 when config.flex unset or Flex errors.  run_daily
│   │                          Step 3c.  --dry-run / --symbol.  Rationale → arch note "Flex Web Service
│   │                          is the durable trade-history source".
│   ├── backfill_flex_trades.py — One-time CLI: parse a hand-exported IBKR Activity Flex Query XML
│   │                          (Trades, Execution detail) → SAME reconcile_fills core.  No Gateway
│   │                          (reads a local FILE, vs reconcile_flex.py which fetches over HTTPS).
│   │                          Idempotent (dedup on exec_id; Flex ibExecID == reqExecutions execId).
│   │                          parse_flex_trades reused by reconcile_flex.py.  --dry-run / --symbol /
│   │                          --source-tz.
│   ├── open_orders.py       — Ops CLI: list open IBKR orders (default, read-only); add
│   │                          --cancel plus --id / --symbol / --all to cancel
│   ├── open_positions.py    — Ops CLI: list held IBKR positions (default, read-only); add
│   │                          --close plus --symbol / --all (+ optional --qty N for a single
│   │                          symbol) to flatten with market sells
│   ├── ingest_news_bodies.py — LLM analyst Phase 1: back-fill news_cache.body with full article
│   │                          text (IBKR reqNewsArticle) for stage-3 universe articles whose body
│   │                          is NULL.  Needs Gateway; fast (~sec/article).  Idempotent.
│   │                          No-op unless config.llm.enabled (or --force).  --symbols/--days/--limit
│   ├── score_news_llm.py    — LLM analyst Phase 2: score un-scored bodies via Ollama →
│   │                          llm_news_analysis.  No Gateway (reads bodies from SQLite); slow
│   │                          (~80s/article, 8B).  Idempotent on (symbol, article_id, model).
│   │                          No-op unless config.llm.enabled (or --force).  --model/--symbols/--limit
│   ├── spike_body_availability.py — THROWAWAY research: measures what fraction of universe news
│   │                          yields a usable FULL body per provider (2026-06-02 verdict: DJ-N
│   │                          ~94% full, premise holds).  --from-universe / --alpaca
│   ├── bench_llm_extraction.py — THROWAWAY research: measures Ollama extraction throughput on this
│   │                          machine (--fetch caches real bodies, --run MODEL benchmarks).  8B
│   │                          measured ~80s/article, 3B ~37s on the dev i5-1334U
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
│   ├── news_dedup.py        — LLM analyst: read-time event clustering.  Groups scored articles by
│   │                          (resolved ticker, day) into events; event score = MEAN of all member
│   │                          reads (re-reports merged); picks a representative for display.  Pure
│   │                          functions (cluster_news_events / jaccard / _tokens), no DB
│   ├── flex_client.py       — IBKR Flex Query Web Service client (stdlib HTTPS): fetch_flex_statement()
│   │                          runs SendRequest (retry on throttle code 1001) → GetStatement (poll on
│   │                          in-progress code 1019) and returns the Trades XML.  Session-independent
│   │                          + retains a year+ — the durable fix for reqExecutions being current-
│   │                          session-only.  http_get/sleep injectable for offline tests.  Consumed
│   │                          by scripts/reconcile_flex.py; config via FlexConfig (env token/query_id)
│   └── ui_queries.py        — @st.cache_data query functions for all dashboard pages;
│                              query_data_status() (ttl=60) aggregates bar counts, news totals,
│                              fundamentals flag, and model checkpoint status per symbol;
│                              query_llm_news_analysis() (ttl=120) resolves attribution + clusters
│                              events at read time for Page 11;
│                              query_trade_forensics()/query_indicator_history()/
│                              query_entry_signal_row()/query_order_decision_for_trade()
│                              assemble the per-trade decision context for Page 10's Trade
│                              Forensics panel (reads get_indicators_history in database.py)
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
│   ├── walk_forward.py      — MLWalkForwardOrchestrator: trains ensemble per fold, bar-by-bar
│   │                          test window, cost model, finbert_coverage tracking, DB persist
│   ├── llm_analyst.py       — LLM news analyst (shadow): Ollama client (JSON mode) + extraction
│   │                          prompt + compute_composite_score (sign × magnitude × novelty-discount)
│   │                          + attribution resolver (primary_entity → ticker via universe names).
│   │                          NOT consumed by signal_runner.  Pure scoring/parsing fns unit-tested
│   └── trade_patterns.py    — Trade Forensics (Page 10) pattern registry: EntryContext +
│                              TradePattern dataclasses + a declarative PATTERNS list (LSTM/MACD
│                              divergence [TEL], RSI+LSTM extreme [UAL], price-outside-bands,
│                              low-conviction squeaker, FinBERT-propped [AZN], high-vol regime),
│                              grouped into buckets.  evaluate(ctx) returns fired patterns;
│                              None-safe (missing inputs never fire / crash).  Pure, no config/DB
│                              import — caller passes the effective gate threshold in.  Read-time
│                              only (nothing persisted); adding a pattern = one list entry.
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
│       ├── 10_Trade_History.py       — Page 10: closed trades from trade_log (WF-simulated +
│       │                              live fills once Phase B lands); summary cards,
│       │                              indicative tax-impact view (ST vs LT, configurable
│       │                              rates in session_state), color-coded trades table,
│       │                              cumulative net-P&L curve + exit-reason donut,
│       │                              per-symbol breakdown, **Trade Forensics drill-down**
│       │                              (symbol→trade selector → hold-trajectory chart [price +
│       │                              bracket + benchmark + model scores], entry decision card
│       │                              with bucketed pattern flags from models/trade_patterns.py,
│       │                              exit attribution, post-exit "left on the table"
│       │                              counterfactual)
│       └── 11_LLM_News_Analysis.py    — Page 11: LLM news analyst (shadow workflow — NOT read by
│                                      signal_runner).  Event-centric/table-first: summary cards,
│                                      sortable+filterable Events table (one row per de-duplicated
│                                      event; mean score; Ticker[resolved] vs Feed[tag] columns +
│                                      Attrib status [match/re-attr/untracked/digest]; status/score/
│                                      magnitude filters), symbol drill-down (daily
│                                      sentiment time series + per-event detail showing the
│                                      underlying article reads), collapsed Research expander
│                                      (distribution, composite-vs-direct scatter, telemetry)
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
│   ├── test_trailing_stop.py   — 8 tests: TrailingStopManager (disabled, idempotent,
│   │                               below/above activation, missing ATR, manual position,
│   │                               short positions skipped, FAILED path)
│   ├── test_llm_analyst.py     — 31 tests: composite-score formula, JSON parsing (fences/garbage),
│   │                               build_result coercion, attribution resolver (name→ticker,
│   │                               mismatch detection, untracked→None)
│   ├── test_news_dedup.py      — 15 tests: jaccard/_tokens, event clustering (Marvell 4→1,
│   │                               entity+day key, representative pick, event_score=mean of reads)
│   └── test_flex_client.py     — 12 tests: Flex Web Service retry/poll state machine (1001 throttle
│                                   retry+exhaustion, 1019 in-progress poll+exhaustion, fatal-error
│                                   short-circuit, empty-statement, missing-creds, Url fallback);
│                                   http_get/sleep injected — offline, no network
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

19 tables in `db/trading.db`. All timestamps are UTC-naive datetimes.

| Table | Key columns | Notes |
|-------|-------------|-------|
| `ohlcv_bars` | symbol, interval, timestamp, OHLCV | unique on (symbol, interval, timestamp); also stores ^VIX |
| `indicator_snapshots` | symbol, interval, timestamp, rsi_14, macd, bb_*, ema_*, atr_14, volume_sma_20 | recomputed from bars by IndicatorEngine |
| `fundamental_data` | symbol, fetched_at, pe_ratio, forward_pe, price_to_book, ev_to_ebitda, revenue_growth, earnings_growth, profit_margin, roe, debt_to_equity, current_ratio, free_cashflow, analyst_target, sector | append-only history (no UNIQUE on symbol — multiple rows per symbol over time); 24h cache in `FundamentalsClient.get` prevents same-day duplicate inserts; readers use `get_fundamentals` (latest row by `fetched_at DESC`) or `get_fundamentals_history` (full series). `sector` (added 2026-06-03) is the raw yfinance GICS label (e.g. "Financial Services") — NULL for pre-migration rows; back-filled by `scripts/backfill_sectors.py`; read + normalised by `risk/portfolio_guard.py:get_sector` via `get_latest_sector` |
| `news_cache` | symbol, article_id, published_at, headline, sentiment_score, body | upsert updates score only when stored score is None. `body` (added 2026-06-02, LLM news analyst) is the full HTML-stripped article text; NULL until back-filled by `scripts/ingest_news_bodies.py` (IBKR `reqNewsArticle`); `set_news_body` only fills it when currently NULL (never overwrites) |
| `signal_log` | symbol, generated_at, bar_timestamp, lstm_score, xgb_score, finbert_score, ensemble_score, regime, signal, passed_gate, gate_reason | written by MLWalkForwardOrchestrator.predict() |
| `ensemble_weight_history` | lstm, xgb, finbert, trigger, recorded_at, symbol, run_id | written after each rebalance; `symbol`/`run_id` added 2026-05-14, pre-migration rows NULL |
| `walk_forward_results` | run_id, symbol, fold_index, train/test dates, sharpe_ratio, max_drawdown, win_rate, n_signals, sentiment_note, universe_policy | sentiment_note + universe_policy added via migration; universe_policy ∈ {`dynamic`, `static`, NULL (pre-2026-05-12)} |
| `universe_assets` | symbol PK, name, asset_class, exchange, is_fixture, stage, market_cap, avg_dollar_volume, stage3_score, active, added_at, last_scored_at, removed_at | dynamic universe candidates. `stage3_score` ∈ [0, 1] — rank-percentile blend of 20-day return + ADV (was `xgb_score` until 2026-05-10) |
| `universe_run_log` | run_id, run_type, stage, symbol_count, duration_seconds, recorded_at, notes | per-stage timing from universe selector |
| `circuit_breaker_log` | event, reason, daily_loss_pct, weekly_loss_pct, triggered_at, reset_at, recorded_at | TRIGGERED / RESET / AUTO_RESET events |
| `equity_snapshots` | snapshot_date (unique), net_liquidation, total_cash, unrealized_pnl, realized_pnl, recorded_at | per-day NLV baseline written once per `signal_runner.py` run (Phase 1) before any orders submit. Re-running the runner same day overwrites the row via `log_equity_snapshot`. The CB's daily/weekly loss-pct math reads these snapshots — without them the auto-trigger has no baseline to compare against |
| `order_decisions` | run_id, symbol, signal, decision, shares, entry/stop/tp prices, position_value, reject_reason, decided_at | per-signal decisions from OrderManager |
| `signal_runner_log` | run_id, run_date, mode, symbols_processed, signals_generated, orders_submitted, orders_rejected, skipped_duplicates, skipped_pending_orders, skipped_stale, longs_closed, trailing_conversions, hold_timeouts, duration_seconds | daily run summaries. `skipped_pending_orders` (added 2026-06-15) counts Phase-4 signals skipped because the symbol already had an unfilled entry bracket working at IBKR — see the "Phase 4 dedups against unfilled entry orders" architectural-decision note |
| `trailing_stop_log` | run_id, symbol, action, shares, entry_price, current_price, atr, trail_amount, reason, decided_at | one row per position evaluated by TrailingStopManager per run (action ∈ CONVERTED / SKIPPED / FAILED) |
| `trade_log` | source ('walk_forward' \| 'live'), run_id, fold_index, symbol, signal, entry_ts, entry_px, exit_ts, exit_px, exit_reason, shares, pnl, pnl_pct, costs_charged, benchmark_return_pct, recorded_at, entry_exec_id, exit_exec_id, parent_order_id, account | closed-trade outcomes: WF bracket simulator (Phase A) + IBKR fill reconciliation (Phase B `source='live'`). exit_reason ∈ stop / tp / trailing / signal_flip / fold_end (WF-only) / manual_close / cb_flatten. `exit_exec_id` is the per-round-trip dedup key (partial unique index `uq_trade_live_exit WHERE source='live'`); `entry_exec_id`/`parent_order_id`/`account` link a live row to its IBKR executions (NULL on WF rows). `benchmark_return_pct` populated by `scripts/backfill_benchmark_returns.py`. Semantics of `pnl` (net), `benchmark_return_pct` (raw SPY), `cb_flatten` inference, and the Phase-B link columns → the corresponding arch-decision notes. |
| `intraday_run_log` | run_id PK, run_timestamp, mode ('intraday'), status ('completed' \| 'gateway_down' \| 'cb_tripped' \| 'error'), daily_loss_pct, weekly_loss_pct, cb_tripped, positions_flattened, trailing_evaluated, trailing_ratcheted, trailing_converted, duration_seconds, error_message | one row per `scripts/intraday_check.py` invocation (12:00 + 15:30 ET). Separate from `signal_runner_log` (different cadence + scope: Phase 1 CB + Phase 3.5 only). `status='gateway_down'` rows have NULL loss_pct and are written even when IBKR is unreachable so missed slots show on Page 8. `trailing_ratcheted` = positions where IBKR moved `Order.trailStopPrice` up since the last `trailing_stop_log` row (the `RATCHETED` action). |
| `fill_log` | exec_id (UNIQUE), order_id, perm_id, parent_order_id, account, symbol, conid, side ('BUY'\|'SELL'), order_type, shares, price, commission, realized_pnl, exec_time, recorded_at | raw IBKR executions ingested by Phase B reconciliation (shipped 2026-05-29) — the audit trail from which `trade_log` `source='live'` rows are aggregated. `exec_id` is the sole idempotency key; only `commission`/`realized_pnl` are ever mutated after insert (commissionReport can arrive on a later fetch than the Execution — `upsert_fill` refreshes them when previously NULL). Written by `execution/reconciliation.py`; populated via `IBKRConnection.get_executions()`. |
| `reconciliation_state` | source, account, last_reconciled_ts, last_run_ts, last_n_fills, notes; UNIQUE(source, account) | Phase B reconciliation watermark (one row per source/account). `last_reconciled_ts` = newest exec_time persisted so far; seeds the next run's window display (NULL first run → now − 7d). Note: IBKR's effective retention is shorter than the 7-day nominal (see arch-decision note) so the watermark is advisory — ingestion takes IBKR's full returned set regardless. |
| `llm_news_analysis` | symbol, article_id, model, provider, published_at, headline, event_type, direction, magnitude, time_horizon, novelty, confidence, entities (JSON), primary_entity, attributed_symbol, summary, rationale, composite_score, llm_direct_score, raw_response, prompt_tokens, output_tokens, duration_ms, parse_ok, scored_at, recorded_at; UNIQUE(symbol, article_id, model) | **LLM news analyst — shadow workflow, NOT read by signal_runner** (added 2026-06-02). One row per (feed symbol, article, model): the 8B's structured extraction + the deterministic `composite_score` (`sign × magnitude × novelty-discount`). `symbol` is the IBKR feed tag; `attributed_symbol` is the company the article is actually about (resolved at READ time, advisory). `llm_direct_score` is a shadow cross-check. Written by `scripts/score_news_llm.py` (Ollama), idempotent. Design + read-time event aggregation → arch-decision note "LLM news analyst is a shadow workflow" + `data/news_dedup.py`. |

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
| `RiskConfig.circuit_breaker_daily_loss_pct` | 0.05 | 5% single-day loss triggers trading halt (raised from 0.03 on 2026-06-11 — CB is the portfolio tail-brake behind the per-position ATR stops, so it sits further out. Rationale → CHANGELOG 2026-06-11) |
| `RiskConfig.circuit_breaker_weekly_loss_pct` | 0.10 | 10% weekly loss triggers trading halt (raised from 0.07 on 2026-06-11 in tandem with the daily bump, keeping the pair coherent) |
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
| `LLMConfig.enabled` | False | Opt-in master switch for the LLM news analyst (shadow workflow). When False, `ingest_news_bodies.py` / `score_news_llm.py` no-op unless `--force` is passed |
| `LLMConfig.model` | `"llama3.1:8b"` | Ollama model tag used for extraction. Measured ~80s/article on the dev i5-1334U; `llama3.2:3b` is ~37s if speed matters more than quality |
| `LLMConfig.ollama_url` | `http://localhost:11434/api/generate` | Local Ollama HTTP endpoint (JSON mode) |
| `LLMConfig.num_predict` | 300 | Max output tokens/article (real extractions emit ~100) |
| `LLMConfig.request_timeout_s` | 1200 | Per-article hard cap |
| `LLMConfig.min_body_chars` | 800 | Skip stub bodies below this (matches the body-availability spike's 'full' floor) |
| `LLMConfig.lookback_days` | 3 | Body-ingest / scoring window |
| `LLMConfig.novelty_discount_floor` | 0.5 | Composite-score novelty multiplier floor: `nov_mult ∈ [floor, 1.0]` (already-known news discounted toward `floor`) |
| `FlexConfig.token` | `""` (env `IBKR_FLEX_TOKEN`) | IBKR Flex Web Service token — **secret** (in `_SECRET_FIELDS`, never written to YAML; prefer `.env`). Generated in IBKR Account Management → Flex Web Service |
| `FlexConfig.query_id` | `""` (env `IBKR_FLEX_QUERY_ID`) | ID of a saved Trades Flex Query (Level of Detail = Execution, period Month-to-Date or Last N Days) |
| `FlexConfig.source_tz` | `"America/New_York"` | Timezone the query's `dateTime` is emitted in; converted to UTC-naive on ingest. This account's queries emit US/Eastern (verified 2026-06-09). Dedup is on `ibExecID` so a wrong tz never double-writes — only shifts shown times |
| `FlexConfig.enabled` (property) | derived | `True` iff both `token` and `query_id` are set; gates `scripts/reconcile_flex.py` (no-op when False) |

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

All pages follow the same patterns:

- **Chart style**: `template="plotly_dark"`, teal `#26a69a` for bullish/positive, red `#ef5350` for bearish/negative, `margin=dict(l=0, r=0, t=40, b=0)`
- **Data access**: all pages query SQLite via `data/ui_queries.py` functions decorated with `@st.cache_data(ttl=300)`. Pages never hit yfinance or the network directly (except Page 2's "Fetch & Score News" button, Page 6's "Refresh from IBKR", and Page 10's Trade Forensics "Fetch latest bars from yfinance" button — all explicit user-triggered escape hatches that backfill the DB then `st.rerun()`).
- **Educational captions**: every chart has a `st.caption()` below (or `st.markdown()` + `st.caption()` before/after) explaining what the chart shows, how to read it, and how it connects to the trading logic.
- **Empty states**: every section has an `st.info()` message when data is absent, explaining what to run to populate it.
- **Sidebar controls**: date range pickers use `config.ml.signal_lookback_days` (default 365) as the default lookback on Page 3.
- **Cache clearing**: sidebar "Refresh cache" buttons call `.clear()` on the relevant `query_*` functions then `st.rerun()`.

Per-page contents are documented once in the **Complete File Structure** section above (`dashboard/` tree, Pages 1–11) — not duplicated here.

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

**Phase 4 dedups against unfilled entry orders, not just filled positions** (`scripts/signal_runner.py:_fetch_pending_entry_symbols`, added 2026-06-15): bracket entries are GTC, so a BUY LMT the symbol gaps past at the open sits unfilled for days. `PortfolioGuard`'s duplicate check only sees *filled* positions (`_fetch_positions` → `quantity != 0`), so without a guard the next daily run regenerating the same signal would stack a **second** bracket. Phase 4 calls `_fetch_pending_entry_symbols(ibkr, loop, positions)` (one `get_open_orders()` call) to build the set of symbols with a working open order but **not** currently held, and skips any actionable signal (or its `EQUIVALENT_PAIRS` partner) in that set. Held symbols are excluded (their open orders are protective TP/STP/TRAIL legs, covered by the duplicate guard + long-only close path). **Best-effort** — returns an empty set when IBKR is unavailable, so it never hard-blocks submission. Per-run `skipped_pending_orders` count → Phase 5 summary + `signal_runner_log`. Motivated by HPE + LRCX 2026-06-15 (two Friday BUY LMTs gapped past Monday's open, unfilled GTC).

**Unfilled entry orders are swept at EOD — entries are good-for-one-session** (`scripts/refresh_recent_bars.py:_cancel_unfilled_entries`, added 2026-06-16; pairs with the Phase-4 dedup note): entry brackets are GTC, so a BUY LMT the symbol gaps past at the open rests for days (CVX 2026-06-15/16). Design choice is **not** to chase gap-aways: the ATR stop/TP are sized off the prior close, so a later fill at a higher price worsens R:R against an unchanged stop. So the EOD refresh (~16:30 ET) cancels every working order on a symbol we do **not** hold — an unfilled entry bracket + its children — so next morning's `signal_runner` re-prices off a fresh close. GTC stays on the entry leg (needed so the ~09:40 pre-RTH placement survives to the open); the sweep just caps an *unfilled* entry's life at one session. **Safety invariant: only NOT-held symbols are swept** (same not-held filter as `_fetch_pending_entry_symbols`; a partial fill → held → excluded). Cancelling the parent tears the bracket down via OCA (a dup cancel is the benign 10148). One `order_decisions` row per swept symbol (`decision='CANCELLED_UNFILLED'`) for Page 8 audit. Gated on `_live_orders_active`; `--no-cancel` to skip; no-op + exit 0 when Gateway unreachable.

**Long-only SELL handling (`allow_short_selling=False`)**: `OrderManager.process()` intercepts SELL signals before position sizing and the portfolio guard when `config.trading.allow_short_selling=False` (the default). If an existing long is found in `positions` → `_close_long_position()` is called (market sell to flatten the position) and `decision='CLOSED_LONG'` is returned. If no long is held → `decision='REJECTED_NO_POSITION'` is returned with no order placed. The guard and position sizer are bypassed entirely for close orders (closing reduces risk; no new sizing is needed). `longs_closed` is tracked in Phase 4 and persisted in `signal_runner_log`. Page 8 shows CLOSED_LONG in purple and REJECTED_NO_POSITION in amber. When `allow_short_selling=True`, the normal bracket-order path runs for SELL signals (future use — currently unreachable in practice). The walk-forward bracket simulator (`models/walk_forward.py:_run_test_window`) reads the same flag and applies the same rule: a SELL from flat is a no-op, a SELL after a long is a close-only signal_flip with no short scheduled. Without this gate, WF aggregate P&L was systematically misleading — 2026-04-30 audit found 66% of simulated trades were short opens that the live runner would never have executed; the long-only subset had +0.06 Kelly while the combined aggregate had −0.08 Kelly.

**Stop-price sanity check in PortfolioGuard**: Check #2 of the 7-check sequential guard (between `circuit_breaker` and `portfolio_drawdown`) verifies the stop price sits on the loss side of the entry price for the given signal — BUY requires `stop < entry`, SELL requires `stop > entry`. Guards against a bad ATR (NaN → 0 → `stop == entry`) or a sign-flip in stop placement, either of which would turn the safety stop into an instant or inverse-direction trigger. Also rejects `entry <= 0` and `stop <= 0`.

**`REJECTED_TOO_SMALL` short-circuits the order flow**: `OrderManager.process()` checks `pos_size.shares < 1` immediately after `PositionSizer.calculate()` and returns `decision='REJECTED_TOO_SMALL'` without calling the PortfolioGuard or submitting any IBKR order. This covers two scenarios: (a) Kelly/fixed sizing produced `position_value < entry_price` (tiny allocation on a high-priced stock); (b) `_get_latest_close()` returned 0 because no bars were cached for the symbol. Without this check, a 0-share "APPROVED" decision would be written to `order_decisions` and (in live mode) IBKR would either reject or silently no-op a 0-share bracket order.

**Circuit breaker is shared state**: `CircuitBreaker` reads/writes the `circuit_breaker_log` table in the shared SQLite DB. The dashboard (Page 8), `signal_runner.py`, and `universe_scheduler.py` all share the same state. The short `ttl=30` on `query_circuit_breaker_status()` means the dashboard reflects reality within 30 seconds without manual refresh.

**Sector classification is data-driven with a hardcoded ETF/fixture tier** (`risk/portfolio_guard.py:get_sector`, added 2026-06-03): two-tier lookup — (1) the hardcoded `_SECTOR_MAP` dict, authoritative for ETFs/fixtures (yfinance gives them no usable GICS sector) + hand-pins; (2) fallback to the latest yfinance GICS sector in `fundamental_data.sector` (captured by `FundamentalsClient._fetch_and_cache`), normalised at read time via `_YF_SECTOR_NORMALIZE` (e.g. "Financial Services"→"Financials", "Communication Services"→"Telecom"). Replaced the prior hand-maintained-only map, which **calcified against the weekly-rotating universe** — by 2026-06-03 ~59% of active symbols (the semi/AI-infra cohort) had rotated in unmapped, so PortfolioGuard's sector cap (check #5) silently passed them all as "Unknown". Returns "Unknown" only when a symbol is in neither tier (or on DB error — degrades safely). `scripts/backfill_sectors.py` filled `sector` on pre-migration rows; new fetches capture it automatically. To override yfinance, hand-pin in `_SECTOR_MAP` (always wins).

**Universe Stage 1 requires Alpaca API keys**: `UniverseSelector._stage1_fetch()` calls `TradingClient.get_all_assets()`. Without keys it raises `UniverseError`. Permanent fixtures are always added regardless, so `run_full()` with no keys will produce a fixture-only list rather than crashing.

**Batch files over persistent scheduler**: Production automation uses `run_daily.bat` (Mon–Fri 09:40) and `run_weekly.bat` (Sunday 01:00) driven by Windows Task Scheduler, not a persistent `universe_scheduler.py --forever` process. Each batch file runs steps sequentially and exits — this avoids silent failures from processes dying overnight, multiple instances on re-login, and race conditions between pipeline and training. Daily training skips existing checkpoints (no-op after first run); weekly uses `--force` for full retraining. `set PYTHONUTF8=1` is set in both batch files to handle Unicode in log output on Windows.

**`get_last_price()` 3-tier market data fallback**: `IBKRConnection.get_last_price()` tries: (1) `reqMarketDataType(1)` live snapshot — requires real-time API subscription in IBKR Client Portal; (2) `reqMarketDataType(3)` 15-minute delayed data — free, no subscription needed, uses `snapshot=False` (required for delayed); (3) yfinance `fast_info.last_price` — always available. Error 10089 (no real-time subscription) is expected without a subscription and triggers automatic fallback to delayed data.

**Phase-4 live-order wiring (`--no-dry-run`)**: `signal_runner._phase4_risk_orders` opens a single event loop and a single `IBKRConnection` at the top of the phase, reuses both for every `OrderManager.process()` call, and closes them in a `finally` block. The loop is set as current via `asyncio.set_event_loop(loop)` **before** `IBKRConnection()` is instantiated — `ib_insync` calls `asyncio.get_event_loop()` during `IB()` construction and raises on Python 3.13 if no loop is bound to the thread. `OrderManager` now accepts an `event_loop` parameter so `_submit_bracket_order` / `_submit_market_close` reuse the same loop instead of creating a fresh one per call. If IBKR is unreachable mid-phase, the runner prints `⚠ IBKR unreachable — falling back to dry-run for this phase` and continues with `dry_run=True`.

**Bracket orders use GTC + tick-rounded prices**: `IBKRConnection.place_bracket_order` applies two fixes that keep brackets alive end-to-end: (1) `round(price, 2)` on entry / stop / TP to satisfy IBKR's minimum-tick-variation check (error 110 — `ib_insync` passes prices through float32 on the wire, which drifts e.g. 202.52 → 202.52000427246094); (2) `leg.tif = "GTC"` on every leg so the bracket survives if the runner fires outside RTH (DAY-TIF orders are immediately cancelled after the 16:00 ET close, which is why error 10349 "Order TIF was set to DAY based on order preset" lost the LMT legs before GTC was added). The $0.01 tick size is correct for all US equities on the current watchlist; sub-penny stocks would need a per-contract tick lookup via `reqContractDetails`.

**STP trigger price lives on `auxPrice`, not `lmtPrice`**: IBKR stores stop-trigger prices in the `auxPrice` field on STP / STP LMT orders; `lmtPrice` is only populated for LMT / STP LMT legs. `OrderResult` has both `limit_price` and `stop_price` fields, and `__str__` renders stops as `@ stop $191.92`. `IBKRConnection.get_open_orders()` returns both. `ib_insync` fills unused price fields with `sys.float_info.max` (~1.8e308) rather than `None` — the `_clean_price()` helper in both `place_bracket_order` and `get_open_orders` treats any non-finite value, anything `> 1e100`, or exact zero as "no price" and returns `None`. Without that sanitisation the Account page renders `$nan` for STP rows.

**Informational error codes continue to expand**: The `informational` set in `IBKRConnection._on_error` now includes `202` (Order Canceled confirmation — logged at ERROR by ib_insync but it's just the ack for a successful `cancelOrder`), `10349` ("Order TIF was set to DAY based on order preset" — IBKR's preset may rewrite TIF even when the client sets GTC explicitly), `10148` ("Order cannot be cancelled — already in Cancelled state" — benign race during cancel+place flows like trailing-stop conversion or long-only close, where the bracket child was already cancelled by IBKR before our cancel arrived; the subsequent place-order call still succeeds), and `10089` ("Requested market data requires additional subscription — Delayed market data is available" — same conceptual fallback as 10167 in a different IBKR wire format; the 3-tier `get_last_price` fallback handles it.  Added 2026-05-21 after the intraday runner produced 13 of these per scheduled slot — one per evaluated symbol — which were correctly handled but flooded the daily log at ERROR level). Full current set: `{2104, 2106, 2107, 2119, 2158, 300, 399, 10148, 10167, 10197, 10349, 202, 10089}`.

**`trade_log.pnl` is already net of costs — `costs_charged` is exposed separately for display only** (`models/walk_forward.py:_close_trade`, `data/ui_queries.py:query_trade_log`). The bracket simulator computes `pnl_pct = gross_pct - total_costs` and writes `pnl = pnl_pct × entry_px × shares`, so the stored `pnl` is the realised *net* dollar P&L. `costs_charged` carries the dollar cost component for display reconstruction. The non-obvious consequence: **never compute `net_pnl = pnl - costs_charged`** — that double-counts fees. The 2026-05-07 SPY verification caught this: a real net −$966.79 trade displayed as −$1,127.10 on Page 10 because `query_trade_log` was subtracting costs that were already deducted upstream. The corrected derivation is `net_pnl = pnl` and `gross_pnl = pnl + costs_charged` (back-derived), now pinned by 6 regression tests in `tests/test_ui_queries.py` (`test_net_pnl_equals_stored_pnl` is the canary — it explicitly fails on the buggy formula). Phase A picked this storage convention so `pnl_pct` stays a valid input for realised-Kelly (Phase C). Phase B's live IBKR fill subscription must follow the same rule when populating `source='live'` rows: write `pnl` as net of commissions/slippage so realised-Kelly across both sources is computed consistently.

**Page 10 dedup sources truth from `walk_forward_results`, not `trade_log`** (`data/ui_queries.py:_keep_latest_run_per_symbol`): each weekly `--force` retrain bulk-inserts a fresh batch of WF trades with new `run_id`s without truncating prior rows, so without dedup the page stacks every historical retrain on top of itself (4× duplicates after 4 weekly runs). The non-obvious choice is *what to dedup against*. Sourcing "latest run_id per symbol" from `trade_log` itself looks tempting but is **wrong**: a fresh training run that produces zero closed trades for a symbol (e.g. long-only gate suppressing every short, no buys firing in the test window) writes no rows to `trade_log`, so the dedup silently falls back to the *previous* run and surfaces stale pre-fix history that the current model no longer produces. The 2026-05-04 verification of the long-only gate caught exactly this: 39 SELL rows from 5 symbols (BA / CHTR / CRCL / IWM / NFLX) survived initial dedup because their 2026-05-03 retrain produced zero closed trades. `walk_forward_results` writes one row per `(run_id, symbol, fold_index)` on **every** fold regardless of trade count, so it always reflects the current training session. Symbols whose latest WF run produced zero trades correctly disappear from the deduped view — that's the right semantic for "no trades in current model" (vs. "stale rows from a previous model"). Live (`source='live'`) rows pass through dedup untouched — every fill is a unique trade. Specific `run_id` filter on the page short-circuits the dedup automatically.

**Trailing stops run as Phase 3.5, after signal generation and before new-order submission** (`scripts/signal_runner.py`, `risk/trailing_stop.py`). `TrailingStopManager.manage()` walks every long IBKR position, finds its bracket's LMT TP + STP legs in `get_open_orders()`, reads `atr_14` from `indicator_snapshots` + latest close from `ohlcv_bars`, and when `current_price ≥ entry + activation_atr × ATR` performs **Cancel-TP → Cancel-STP → Submit-TRAIL**. The order is intentional: submitting TRAIL first would briefly leave STP+TRAIL both live in different OCA groups (a gap-down could trigger both → unintended short); the current order leaves a sub-second "no stop" window instead, acceptable in normal markets. Idempotent (existing TRAIL → skipped). Skipped entirely in dry-run / `paper_orders_enabled=False` / `trailing_stop_enabled=False` (default — opt-in). TRAIL uses `orderType="TRAIL"`, `auxPrice=trail_amount` (rounded $0.01), `tif="GTC"`; `stop_price` carries the trail distance. Shorts skipped (long-only).

**Hold-timeout runs as Phase 3.6, after trailing stops and before new-order submission** (`scripts/signal_runner.py:_phase3_6_hold_timeouts`). For each held long, queries `signal_log` for the most recent passed-gate BUY (`data.database.get_latest_buy_signal_ts`); if older than `config.risk.max_hold_days` calendar days, flattens with a market sell after cancelling bracket children (LMT TP / STP / STP LMT / TRAIL — 4-type filter, since Phase 3.5 may already have converted some). Placement between 3.5 and 4 = "let winners trail, let stale ones go, then take new entries" (before 3.5 would cancel same-morning TRAIL conversions; after 4 would emit a fresh bracket before flattening). Symbols with NO BUY in `signal_log` are skipped (manual/pre-history holdings have no staleness anchor). Skipped in dry-run / `paper_orders_enabled=False` / `hold_timeout_enabled=False` (default — opt-in) / `max_hold_days <= 0` (defensive). Closures → `order_decisions` `decision='CLOSED_TIMEOUT'`, count → `signal_runner_log.hold_timeouts`. The "re-confirming signal" semantic (vs a pure time-based stop) preserves winners the model still likes.

**Fold-end closures are backtest artifacts, not strategy decisions** (`models/walk_forward.py:_run_test_window`, surfaced by `dashboard/pages/10_Trade_History.py` benchmark section): `exit_reason='fold_end'` rows are positions force-flattened at the last bar of a WF test window because no stop / TP / trailing / signal_flip had fired by that bar.  They are mechanically correct (the bracket simulator must close every position before the next fold begins, otherwise positions would leak across fold boundaries and contaminate the next fold's training data with implicit lookahead) but they are **not exit decisions the live system would ever make** — live trading has no fold boundaries.  Two consequences that compound:
- **Left-truncation bias toward winners-still-running**: in a rising market, fold_end exits are disproportionately positions whose ATR stops did NOT fire (winning at test_end); the stops fired on losers earlier in the same fold.  So `fold_end` shows *positive* excess while the strategy-decided subset (stop/tp/signal_flip/trailing) shows *negative* excess — opposite signs, same window.
- **fold_end is ~half of `trade_log`** (~48% on baselines) — excluding it removes nearly half the data, and the negative-excess picture only becomes visible after it's removed.  Sample remains ample.

**Dedup vs raw views are honest answers to different questions** (`data/ui_queries.py:_keep_latest_run_per_symbol`, surfaced by Page 10's `dedup_to_latest_run` checkbox).  Same `trade_log` rows, two legitimate aggregations:
- **Deduped** (default ON): keeps the latest WF run_id per symbol → "what does the current model do?"  Positive cumulative excess.
- **Raw** (toggle OFF): every weekly `--force` retrain stacks a fresh batch → "how has the strategy behaved across all model versions ever trained?"  Strongly negative cumulative excess.  "Cumulative excess" is the unweighted sum of per-trade `excess_pct` × 100 (`dashboard/pages/10_Trade_History.py`), NOT a portfolio-weighted compound return — magnitude scales linearly with trade count.

The two diverge by ~1000+ pp on the same fold_end-excluded slice — neither is wrong; Page 10 defaults to deduped.  The −7% avg-excess **stop bleed** is the only finding that survives both views (same sign + magnitude).  Current baseline numbers + the divergence figure live in `docs/findings/stop_bleed.md` / followups.md, pinned by `tests/test_trade_log.py::test_benchmark_aggregates_*_baseline_<date>` — run those as a canary when touching dedup logic, the backfill, or SPY ingestion (the pin date bumps every Sunday `--force` retrain).

**Benchmark-relative tracking uses raw SPY return vs net trade P&L** (`data/ui_queries.py:query_benchmark_returns`, `scripts/backfill_benchmark_returns.py`).  The asymmetry is deliberate:
- The trade's `pnl_pct` is **net** of commissions and slippage (Phase A's `pnl is net` schema convention — see `_close_trade` and the dedicated architectural-decision note above).
- The benchmark's `benchmark_return_pct` is **raw**: `(SPY_close_exit / SPY_close_entry) − 1`.  No fee adjustment.

This is the correct retail-alpha frame: the counterfactual is "would I have done better holding SPY?" — and a buy-and-hold SPY position incurs no trading fees, so the comparison "my P&L net of friction vs frictionless benchmark" is exactly what the retail investor faces.  Computing `excess = pnl_pct − benchmark_return_pct` does NOT need any cost adjustment.  The double-count footgun to watch for: `excess = (pnl_pct − costs_charged) − benchmark_return_pct` subtracts costs *again* from a `pnl_pct` that already excluded them.  Pinned by `tests/test_ui_queries.py::test_excess_return_uses_net_pnl`.

**Page 9 cross-references live IBKR open orders so cancelled bracket legs don't render as if still alive** (`dashboard/pages/9_Account.py:_enrich_positions`). `get_latest_risk_levels` reads the *original* bracket prices from `order_decisions`, but those go stale once `TrailingStopManager` converts a TP→TRAIL in Phase 3.5 (original LMT/STP cancelled at IBKR) — without the cross-ref the page rendered SNOW's cancelled TP/stop as still-protecting (2026-05-19 stale-TP report). The function takes `open_orders` and, per position, checks for a matching SELL LMT (active TP) / SELL STP|STP LMT (active stop) / SELL TRAIL; a leg *not* in `open_orders` is blanked ("—" via `na_rep`). TRAIL surfaces `trail_amount` (=`auxPrice`, the distance) + best-effort `trail_trigger` (=`Order.trailStopPrice`, the live ratcheting stop — `None`→"trigger pending IBKR update" until the first ratchet). Back-compat: `open_orders=None` skips the cross-ref (legacy behaviour; protects tests + tolerates a transient orders-fetch failure). New `get_open_orders` field `trail_stop_price` exposes `Order.trailStopPrice`; `stop_price` still carries the trail distance on TRAIL rows.

**Intraday Phase 3.5 reads price from IBKR, not the cached daily bar** (`scripts/intraday_check.py`, `risk/trailing_stop.py:manage(price_source=...)`).  The daily 09:35 ET runner calls `manage()` with no `price_source` → reads the latest `ohlcv_bars` row (`get_bars(..., limit=1)`).  The intraday runner can't use that (mid-day the row is yesterday's close, 18+ h stale), so it passes a `price_source` callable wrapping `IBKRConnection.get_last_price()` (3-tier fallback), called once per evaluated long.  Without it, ratchet detection compares live `Order.trailStopPrice` against a stale-bar price → false positives.  ATR still comes from `indicator_snapshots` (daily-derived, doesn't change intraday) so the trail distance is "ATR-as-of-last-completed-bar".  Back-compat: `price_source=None` preserves the DB-read path (pinned by `test_trailing_manager_default_uses_db`).

**Intraday TP→TRAIL conversion is gated behind a separate config flag + a buffer above the daily activation threshold** (`config/settings.py` → `RiskConfig.intraday_trail_conversion_enabled` + `intraday_conversion_buffer_atr`).  The intraday runner *could* convert at 12:00/15:30 ET like daily Phase 3.5, but it's opt-in (default off) because (1) the cancel→cancel→submit "no stop" window is riskier mid-session than at the calmer open, and (2) marginal cases at noon were just evaluated at 09:35 against the same daily ATR.  `intraday_trail_conversion_enabled=False` ⇒ intraday only emits `RATCHETED`/`SKIPPED`.  When on, the activation threshold tightens by `intraday_conversion_buffer_atr × ATR` (default 0.5) on top of `activation_atr`, keeping conversions to genuinely-strong moves.  The `intraday=True` kwarg on `manage()` engages the gate; the daily runner never passes it.

**Intraday runner exits 0 on Gateway-down rather than raising** (`scripts/intraday_check.py:run` + `main`).  On connect failure (or `IBKRConnection()` construction raising) the runner writes a `status='gateway_down'` row, logs a WARNING, and **exits 0**; the outer `main()` `try/except BaseException` likewise writes `status='error'` and exits 0.  Why: Task Scheduler treats non-zero exits as failures and retries — against an already-flaky overnight-logged-out Gateway that would spin into a noise storm.  Exit 0 keeps the scheduler quiet but the row makes the missed run visible on Page 8 (silent skip ≠ invisible skip).  Exit-0 invariant defended in three places: (a) the gateway-down branch writes its row in its own `try/except`; (b) `main()` catches `BaseException`; (c) the fallback `print` is ASCII-only so a `_force_utf8_streams()` failure can't re-raise.  Pinned by `test_intraday_runner_gateway_down_exits_clean` + `test_intraday_runner_top_level_exception_writes_error_row`.

**IBKR `reqExecutions` retention is unreliable — the in-session poll is best-effort, the Flex backstop is durable** (`execution/ibkr_connection.py:get_executions`, Phase B).  The 2026-05-29 Phase B verification first found `reqExecutions` dropping fills only 2 days old; the 2026-06-09 probe then established it is **session-only** (see the "Flex Web Service is the durable trade-history source" note, which supersedes the original "shorter-than-7d" framing).  **Operational consequence that still stands:** the same-day in-session poll cannot be relied on across a Gateway reset, which is exactly why the T+1 Flex reconciliation (`run_daily.bat` Step 3c) exists as the durable path.  Do not assume the in-session window is forgiving when reasoning about missed runs or backfill windows.

**Flex Web Service is the durable trade-history source; `reqExecutions` is current-session-only** (`data/flex_client.py`, `scripts/reconcile_flex.py`, `config.flex`; added 2026-06-09).  A 2026-06-09 probe established that the entire IBKR real-time API tier (`reqExecutions` with/without `ExecutionFilter.time`, `reqCompletedOrders`, `ib.fills()`/`trades()`) returns **only the current Gateway session's** executions — there is no historical store in that tier.  Since this Gateway resets overnight, the ~10am Phase-1 poll reliably `fetched 0`.  **The durable fix is the IBKR Flex Query *Web Service*** — a server-side statement service, session-independent, retains a year+.  `data/flex_client.py:fetch_flex_statement` runs the 2-call REST flow (`SendRequest` → poll `GetStatement`); `scripts/reconcile_flex.py` parses with the **same** `backfill_flex_trades.parse_flex_trades` + feeds the **same** `reconcile_fills` core (dedup on `ibExecID` == `reqExecutions` `execId`).  Wired as `run_daily.bat` **Step 3c** (before 3b).  Client handles: (1) `SendRequest` rate-throttle (error **1001**) → retry w/ backoff; (2) async generation (error **1019**) → poll; (3) **Flex is T+1** so it recovers prior-day fills while the in-session poll stays the same-day path (complementary, not redundant).  Distinct from `scripts/backfill_flex_trades.py` (parses a hand-exported XML; one-off).  Token is a secret (`FlexConfig.token` ∈ `_SECRET_FIELDS`, env/`.env` only).  Full incident → CHANGELOG 2026-06-10.

**`get_executions` passes an empty `ExecutionFilter()` and bounds client-side — no server-side time filter** (`execution/ibkr_connection.py:get_executions`).  `ExecutionFilter.time` is too brittle: a bare `'yyyymmdd HH:MM:SS'` triggers IBKR warning 2174 + an implied-tz window shift; the recommended `'…UTC'` form was observed (2026-05-29) to **silently return zero rows**.  So the reconciler bounds/aggregates client-side on the normalised UTC `exec_time` (`_to_naive_utc` in `execution/reconciliation.py` coerces UTC before stripping tzinfo).  **Do not re-add a server-side `time` filter** — the payload is tiny and the filter's failure modes are silent.

**LLM news analyst is a shadow workflow, deliberately decoupled into ingest vs score** (`scripts/ingest_news_bodies.py`, `scripts/score_news_llm.py`, `models/llm_analyst.py`, `data/news_dedup.py`, Page 11 — added 2026-06-02).  A local 8B model (Ollama) reads *full article bodies* (which FinBERT never sees — `news_cache` was headline-only) and produces structured extraction + a sentiment score.  **Nothing in `signal_runner` reads it** — it's a parallel research signal surfaced only on Page 11.  Five deliberate design choices, each driven by what the build verified:
- **Batch, not daemon.**  Matches the "batch files over persistent scheduler" decision.  Two *separate* steps: body ingestion (needs Gateway, fast — piggybacks the morning window) and LLM scoring (no Gateway — reads bodies from SQLite, slow at ~80s/article).  Decoupling means the expensive scoring step never depends on Gateway uptime and can run anytime, off the pre-market critical path.
- **The body-availability premise was measured first** (`scripts/spike_body_availability.py`, 2026-06-02): IBKR `reqNewsArticle` returns a usable full body for **~94%** of universe news, and — contrary to expectation — Dow Jones (`DJ-N`) bodies come through full (median ~3,400 chars), not as paywall stubs.  The throughput was measured too (`scripts/bench_llm_extraction.py`): 8B ~80s/article, 3B ~37s on the dev i5-1334U (a 15W ultrabook), via Ollama's exact prefill/decode token counts.  ~100 output tokens/article (terse JSON), so a typical news day fits an overnight window.
- **Score = deterministic composite from structured fields, NOT an LLM-emitted number** (`compute_composite_score`): the LLM classifies direction/magnitude/novelty (what it's good at); we compute `sign × (magnitude/5) × novelty_discount` ourselves.  This is transparent (the dashboard shows the decomposition), tunable, and stable.  `llm_direct_score` (the model's own [-1,1] guess) is stored only as a shadow cross-check.
- **Attribution: `primary_entity` (company name) → ticker, resolved at READ time into a four-way status** (`resolve_attribution_status` + `build_company_name_map`).  IBKR symbol-tagged news is *frequently about a different company* — verified live: 7 of 8 NVDA/AAPL-tagged test articles were actually about Marvell/Broadcom/Dell/HPE.  The resolver maps names→tickers via `universe_assets.name` (Marvell→MRVL, Broadcom→AVGO) and classifies each article as one of `matched` (about the feed symbol) / `reattributed` (a *different* tracked ticker — the headline value-add the FinBERT path misses) / `untracked` (a company we don't follow, e.g. HPE→None) / `digest`.  Read-time resolution means the heuristic can improve without re-running the 8B.  The matcher is deliberately conservative (exact ticker, or token-set match against real company names; bare-ticker variants never fuzzy-match) after a substring version false-matched "Nvi**dia**"→DIA.  **Only `reattributed`/`untracked` are "mismatches"** (`status_is_mismatch`); the older boolean `resolve_attribution` is retained as a back-compat wrapper over the status function.
- **Multi-company digests are detected and excluded from per-ticker sentiment, NOT flagged as mismatches** (`is_digest` + `_DIGEST_HEADLINE_PATTERNS` in `models/llm_analyst.py`; added 2026-06-03).  Recurring DJ roundups — *Substantial Insider Sales: Morning Report*, *Comex … Delivery Intentions*, Barron's "…and More Stocks That Explain Today's Market" — enumerate dozens of companies, so the 8B's single `primary_entity` pick is arbitrary and the per-company sentiment is noise.  Found via the NXPI report 2026-06-03: an insider-sales digest tagged NXPI (because NXP is one of ~50 line items) was flagged "mismatch — about ACEL", which read as a false positive (the feed tag IS legitimately in the article).  Detection is **headline-based** (high precision; the body isn't in the dashboard read frame) and broadcast-safe (the same digest arrives under many feed tags; one headline rule reclassifies every copy).  Digests resolve to `attributed_symbol=None`, get `ticker=NA` on Page 11 (dropped from the per-ticker drill-down + sentiment time series), and `data/news_dedup.py:_event_key` routes them to a `digest:<headline>` namespace so broadcast copies merge into one event and never collide with a real ticker's bucket.  Extend the pattern list as new recurring formats surface; an entities-count / tabular-body heuristic was deliberately deferred (entities lists are capped low, so a count threshold would either false-positive on single-company stories or never fire).
- **Event de-duplication groups by (resolved ticker, day), NOT text similarity** (`data/news_dedup.py`).  The same event is re-reported many times and the 8B scores near-duplicates inconsistently (observed: four "Marvell surges" articles scored +0.00/+0.90/+0.00/+0.90).  Text-similarity clustering was tried and **abandoned on real data**: intra-event Jaccard ran as low as 0.14 while a *different*-event pair (Marvell-vs-Broadcom) hit 0.19 — short reworded headlines share generic chip-sector vocabulary, so no threshold separates them.  The resolved ticker is the reliable signal.  **Event score = MEAN of all member reads** (every read counts — MRVL → +0.45, not the +0.00 a single representative gave); a representative article is picked (highest confidence) only to choose which headline/summary to *display*.  Known trade-off: two genuinely different same-day stories about one company merge — far less harmful than re-report inflation, and `event_size` + the score spread keep it visible.

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
- **CB-flatten market closes mislabeled in `trade_log.exit_reason`** *(code complete 2026-06-11 — awaiting live verification)* (`execution/reconciliation.py:_infer_exit_reason`, `data/database.py:has_cb_flatten_near`): circuit-breaker liquidations (`scripts/intraday_check.py` → `risk/order_manager.flatten_all_longs`) cancel a position's bracket children and submit a plain MKT sell, leaving no signal the exit-reason waterfall recognised — so it labeled them off whatever stale state it found: a `trailing_stop_log` CONVERTED row → `trailing`, else fall-through → `manual_close`. **Observed 2026-06-10 (first-ever live full-portfolio CB flatten — 17 positions, daily −3.30%):** all 17 had authoritative `order_decisions.decision='CB_FLATTENED'` rows, yet reconciliation (via Flex on 6/11) labeled 15 as `manual_close` and **C/GLW as `trailing`** — even though GLW never converted to a trail (its `trailing_stop_log` shows only SKIPPED). Impact: Page 10's exit-reason donut shows forced liquidations as discretionary `manual_close`, and the trailing-stop attribution gained a phantom **+$3,061 C "win"** and **−$2,726 GLW "loss"**, contaminating the active "Trailing stop crowded out" investigation (which needs accurate per-exit trailing P&L). **Fix:** new `has_cb_flatten_near(symbol, ts, minutes=5)` DB helper (a `CB_FLATTENED` order_decision within ±5 min of the fill — the decision lands within ~1s and CB flattens are rare, so the tight match is unambiguous) checked in `_infer_exit_reason` as a NEW step 2, ahead of the trailing-log / price-match / default branches, returning a distinct `exit_reason='cb_flatten'`. The 17 mislabeled 6/10 rows were backfilled in place (15 `manual_close` + C/GLW `trailing` → `cb_flatten`; META stop untouched); Page 10 donut color map + filter legend gained the `cb_flatten` value. **Status:** implemented. Test coverage: 3 new in `tests/test_reconciliation.py::TestCBFlattenExitReason` (decision-near-exit → cb_flatten / wins-over-stale-trailing-log [the C/GLW case] / no-decision-unaffected). Full suite: 447 passed. Verification pending: the next live CB-flatten reconcile should write `live trade: … reason=cb_flatten source=cb_flatten` (grep the daily/Flex log) instead of `manual_close`/`trailing`.
- **Phase B exit-reason waterfall misclassifies gap-through bracket fills as `manual_close`** *(code complete 2026-06-02 — awaiting live verification)* (`execution/reconciliation.py:_infer_exit_reason` step 3): the `order_decisions_price_match` step matched a closing fill to the recorded bracket stop/TP within a symmetric `tol = max(0.05, exit_px * 0.001)` (~$0.26 on a $256 stock). On a gap-up open a long's TP LMT fills *above* the TP level by the gap size, so `|exit_px - tp|` far exceeds the tolerance, the match fails, and the trade falls through to the `default` branch → `manual_close`. This misfires on exactly the off-session gap-through scenario Phase B exists to capture. **Observed 2026-06-02:** MRVL — the first organic Phase B paired round trip — exited $255.90 on a +15.5% gap-up open (recorded TP $244.89, gap $11.01); stored `exit_reason='manual_close'` though it was almost certainly the bracket TP (consecutive order IDs 222 entry / 223 exit; price never reached the TP before the gap; fill at the opening cross). pnl is unaffected, but the wrong label pollutes Page 10's exit-reason donut + per-reason excess table, `docs/findings/tp_concentration.md`, and future realised-Kelly-by-exit-reason. Full write-up: `docs/case_studies/mrvl_2026-05.md`. **Fix:** made step 3 directional and gap-aware for a long exit — classify `tp` when `exit_px >= tp - tol` (fill at-or-above TP) and `stop` when `exit_px <= stop + tol` (fill at-or-below stop); prices between stop and TP match neither and fall through to default. **Status:** implemented; the MRVL row (`trade_log` id=2020) was hand-corrected to `exit_reason='tp'` after the fix landed. Test coverage: 3 new in `tests/test_reconciliation.py::TestGapThroughExitReason` (gap-up-above-TP → tp [the MRVL case] / gap-down-below-stop → stop / mid-bracket → manual_close over-match guard); existing `TestPriceMatchToleranceRegimes` still passes. Full suite: 311 passed + 1 skipped. Verification pending: the next organic off-session gap-through fill should reconcile with the correct `tp`/`stop` reason (grep the daily log's `live trade: … reason=`). Currently long-only; a symmetric short-exit rule is needed if `allow_short_selling` is ever enabled.
- **Between-run bracket/trail/TP fills are invisible to all local tables until Phase B reconciliation ships** *(Phase B shipped 2026-05-29; historical gap closed 2026-05-31 via Flex backfill — awaiting first organic daily round trip)* (`scripts/signal_runner.py` Phase 2 held-only set, `data/ui_queries.py`, `trade_log`): GTC bracket legs (STP/LMT/TRAIL) fill between daily runs at IBKR, but nothing in the codebase ingests `reqExecutions`, so a filled exit leaves no `CLOSED_LONG`/`order_decisions`/`trade_log` row — the position simply disappears from the next run's IBKR positions list. The daily-review position-diff is currently the *only* signal these exits happened, and it can only *infer* price/reason from OHLCV bars, not confirm shares/fill price/realised P&L. Four confirmed instances in 9 days across all three exit mechanisms: AON 2026-05-21 (trail), SNOW 2026-05-21 (trail), SCHW 2026-05-27 (fixed stop, −5.7%), SPY 2026-05-29 (TP, +2.39%) — none reconstructable from local DB. Impact: realised P&L, win rate, and realised-Kelly inputs are all blind to every winning TP and losing stop that fires off-cycle, which is the majority of exits. **Fix:** ship Phase 4.5 Phase B polling reconciliation (`reqExecutions` at the start of each `signal_runner` Phase 1 → `fill_log` ingest → `trade_log` round-trip aggregation per the documented Phase B schema), which tolerates Gateway downtime by design and back-fills the 7-day execution window. This escalation moves Phase B from "next major roadmap item" to "priority." See the "Phase 4.5 — Realised P&L plumbing" enhancement entry below for the full Phase B schema + reconciliation flow. **Status (2026-05-31):** two-part resolution. (1) **Going forward** — Phase B shipped 2026-05-29 (`reqExecutions` polling in `signal_runner` Phase 1), so off-cycle fills inside IBKR's ~7d retention are now captured automatically; the daily-run reliability requirement (see the "reqExecutions retention is shorter than documented" arch-decision note) is what keeps this from regressing. (2) **The historical backlog** — all 4 named invisible exits (and the rest of the year's fills) had *already aged out* of the 7d window before Phase B's first run, so polling could never recover them. They were instead recovered via a one-time IBKR Activity Flex Query export run through `scripts/backfill_flex_trades.py` (Flex retains a year+): **23 `source='live'` round trips written**, P&L validated against IBKR's own `fifoPnlRealized` to the penny on 22/23 (SPY off $1.85 — a pre-existing Phase B fill whose commission the dedup correctly declined to overwrite). Page 10's live view + benchmark-relative section now populate from real fills. **Remaining gap:** the *daily automatic* path hasn't yet organically written a round trip — the 4 currently-open positions (COHR/MRVL/WDC/USO, sitting in `fill_log` as entry-only) haven't exited. The first such exit closes the loop end-to-end; tracked in `docs/reviews/followups.md` (2026-05-29 Phase B item). This entry stays out of CHANGELOG.md until that organic round trip lands.
- **Reconciliation defers an entry-only orphan that is actually flat at the broker, silently dropping a closed trade** *(code complete 2026-06-08 — awaiting live verification)* (`execution/reconciliation.py:_aggregate`): when a `source='live'` position's entry fill is in `fill_log` but its exit fill is never ingested (the exit aged out of `reqExecutions` before any daily run polled it — e.g. a Friday exit invisible by Monday after a weekend Gateway session reset), `_aggregate` ends the walk with `net>0` and logs `<SYM> not flat at window end — open position / partial, leaving fills for a later run`. It could not distinguish "still held, exit pending" from "closed at the broker, exit fill missed," so a closed position's realised P&L was deferred indefinitely and never recovered via the polling path — the inverse of the net<0 lone-exit detector shipped 2026-06-05. **Observed 2026-06-08:** GE (CLOSED_LONG 6/05, 139 sh @ $325.88, entry $283.21 ≈ **+$5,928.17 / +15.1% winner**) and VRT (120 sh @ $331 entry, STP exit ≈ **−$4,258.45 / −10.7%**) both sat as entry-only orphans (GE net=139, VRT net=120, no SELL fill) with no `trade_log` row, while both were absent from the live IBKR positions list — i.e. flat at the broker — because that day's reconciliation `fetched 0 execution(s)` (the whole post-6/05 fill set, incl. the GLW/INTC/ON 6/05 entries, was missing from `reqExecutions`). Both were recovered the same day via `scripts/backfill_flex_trades.py logs/Trade_History_20260608.xml` (GE `trade_log` id=2185, VRT id=2186; penny-perfect vs IBKR `fifoPnlRealized`). **Fix:** `reconcile_fills` gained a `live_positions` param (the broker's held symbols, fetched in `signal_runner._phase1_reconcile_fills` via `_fetch_positions` on the already-open connection and passed in); `_aggregate` now splits the `net>0` orphan branch — a symbol absent from `live_positions` is flagged as a missed exit (`n_missed_exits` count + a distinct WARNING prompting Flex recovery) instead of being deferred. `live_positions=None` (Flex-backfill path, tests, any caller without broker state) preserves the legacy "leaving fills for a later run" behaviour exactly. **Status:** implemented. Test coverage: 5 new in `tests/test_reconciliation.py::TestMissedExitDetection` (flat-at-broker flagged / still-held not flagged / no-live-positions legacy / partial-still-held not flagged / completed-round-trip unaffected). Full suite: **425 passed** (was 420). Verification pending: a live daily run where a between-run exit has gone flat-at-broker without its exit fill ingested — Phase 1 should print `⚠ N missed exit(s) (flat at broker, exit fill not ingested — Flex-recover)` and the daily-review §4 should pick it up from the WARNING rather than only from the position-diff. (Does NOT fix the *upstream* cause — `reqExecutions` returning 0 for the prior session's fills; that scheduling/retention gap is tracked separately in `docs/reviews/followups.md` 2026-06-08.)
- **Trail manager implicitly uses intraday quotes when Phase 2 refresh runs mid-morning** *(partial fix 2026-05-20 — intraday runner shipped option (c); daily-path fix deferred)* (`risk/trailing_stop.py`, `scripts/signal_runner.py:_phase2_data_refresh`): Phase 2 calls yfinance at ~10:06 ET, after the regular session has opened, and writes the live intraday quote as today's "1d close" in `ohlcv_bars`. Phase 3.5 then reads `get_bars(symbol, "1d", limit=1)` and uses that intraday value as the price input to the activation check. The trail manager's docstring (and CLAUDE.md "Trailing stops run as Phase 3.5") imply EOD-close semantics, but in practice the input is an intraday print. **Observable consequence:** all three trailing-stop conversions in system history performed their activation check against Phase 2's partial-bar close, not a prior-day EOD close — SNOW 2026-05-07 ($154.81 morning print), AON 2026-05-19 ($327.39 morning print, ~$0.88 above activation), ASTS 2026-05-20 ($87.28 morning print, $3.22 above activation). Had Phase 2 not run on any of those mornings, all three would have evaluated against the prior-day close and the trail would have stayed SKIPPED — for ASTS specifically, 5/19's close of $80.57 would have left it below activation $83.69. The trade still exits via SELL signal in Phase 4 either way for ASTS, but the framing matters: trail activation today is implicitly an intraday-driven event masquerading as EOD logic, not by design. **Fix considerations:** (a) document the current behaviour explicitly in CLAUDE.md and `trailing_stop.py` docstring (cheapest); (b) refactor the trail manager to read the *prior* daily bar via `get_bars(symbol, "1d", limit=2)[0]` so EOD-close semantics are enforced (more conservative; would have left all three conversions SKIPPED until the next morning's evaluation after `run_eod.bat` writes the finalised close); (c) move trail evaluation explicitly to an intraday cadence (the "Intraday lightweight runner" enhancement) so the input timing is by design.  **Status (2026-05-20):** intraday runner shipped option (c) — `scripts/intraday_check.py` evaluates trailing stops at 12:00 ET / 15:30 ET against `IBKRConnection.get_last_price()` (NOT the cached daily bar), so the intraday codepath has correct-by-design input timing. The daily-runner codepath at 09:35 ET still reads from `ohlcv_bars` and still inherits the Phase-2-partial-bar issue described above — that codepath is unchanged.  **Daily-path fix deferred** between option (b) [enforce EOD semantics via the new `price_source` hook by passing a "prior-day close" callable from the daily caller] and option (d) [remove daily-runner trail evaluation entirely; intraday slots own it].  Option (d) only becomes safe once the intraday runner has demonstrated operational reliability — otherwise removing the daily path risks leaving trail evaluation gapped on intraday-failure days.  **Trigger to revisit (daily-path fix):** after ≥4 weeks of operating the intraday runner.  If `intraday_run_log` shows `status='completed'` runs reliably (say >80% of scheduled slots), option (d) becomes attractive — single canonical path, removes the buggy code instead of working around it.  If intraday Gateway-down events are common, option (b) is the safer pick — leaves the daily-runner trail evaluation in place but anchors it to a non-stale price input.  Don't lock in either now; the choice is a function of operational data we don't have yet.
- **Kelly disconnected from realised outcomes** *(code complete 2026-05-07 — awaiting live verification)* (`risk/position_sizer.py`): Kelly fraction was derived from `|ensemble_score|` as a P(win) proxy; the entire risk stack never saw actual fills or P&L. **Status:** addressed by Phase 4.5 Phase C — see "Status (Phase C — 2026-05-07)" under the Phase 4.5 entry below for the full implementation. `compute_realised_kelly` now reads `trade_log.pnl_pct` for empirical win-rate / avg-win / avg-loss; `PositionSizer` engages the realised path at `n_trades >= min_trades_for_realised_kelly` (default 30) with a cold-start proxy fallback. Verification pending: Sunday `--force` retrain with at least one symbol clearing 30 trades to observe `method='kelly_realised'` actually drive a non-fallback share count in `trade_log`. Live (`source='live'`) Kelly is gated on Phase B and starts tracking realised broker fills the moment that lands.
- **Stale-price signals accepted** *(code complete 2026-04-28 — awaiting live verification)* (`signal_runner.py` + `risk/order_manager._get_latest_close`): `get_bars(..., limit=1)` returns the most recent cached bar regardless of age. If the pipeline hasn't run for a week, signals fire against week-old prices. Fix: gate each symbol on `latest_bar_age < max_stale_days` (config) before passing to `OrderManager.process`. **Status:** implemented via new `RiskConfig.max_bar_staleness_days` (default 3) + a stale-bar gate at the top of `_phase3_signals` that drops symbols whose newest cached daily bar is older than the limit. `skipped_stale` count surfaces in Phase 5 print + `signal_runner_log.skipped_stale`. Unit tests pass (2 new in `test_signal_runner.py`: fresh-pass and stale-drop). Verification pending: needs a real-world run after a deliberate pipeline-skip (e.g. one weekend without `run_pipeline.py`) to confirm the gate fires correctly against actual stale data and that the dashboard shows the skipped count.
- **No HOLD-timeout exit rule** *(code complete 2026-05-19 — awaiting live verification)*: once a BUY fills, the position sits until an explicit SELL signal fires. In sparse-signal regimes a position can hold indefinitely. Fix: add a config-driven "flatten after N bars without a re-confirming signal" rule in `signal_runner.py`, or a time-based stop alongside the ATR stop. **Status:** implemented as **Phase 3.6** in `scripts/signal_runner.py:_phase3_6_hold_timeouts`, sequenced after Phase 3.5 (trailing stops) and before Phase 4 (new orders). The "re-confirming signal" semantic was picked over a pure time-based stop: each held long is checked against `signal_log` for the most recent passed-gate BUY via the new `data.database.get_latest_buy_signal_ts(symbol)` helper; if the latest BUY is more than `config.risk.max_hold_days` calendar days old, the position is flattened with a market sell after cancelling its bracket children (LMT TP, STP, STP LMT, and TRAIL legs — same pattern as `OrderManager._cancel_bracket_children` to prevent orphaned stops firing against zero shares and opening unintended shorts post-close). Symbols with NO BUY history in `signal_log` are explicitly skipped (manual positions / pre-history holdings have no anchor for "stale"). Two new `RiskConfig` knobs gate the feature: `hold_timeout_enabled: bool = False` (opt-in, mirrors `trailing_stop_enabled` pattern) + `max_hold_days: int = 30` (~22 trading days; 0 short-circuits as a defensive guard). Phase 3.6 is skipped entirely in dry-run, when `paper_orders_enabled=False`, or when either knob disables it. Each closure is persisted to `order_decisions` with `decision='CLOSED_TIMEOUT'` so Page 8 can surface a retrospective view alongside CLOSED_LONG / APPROVED rows. New `signal_runner_log.hold_timeouts` column (idempotent `_migrate()` ALTER) tracks the per-run count and is printed in Phase 5. Test coverage: 8 new in `tests/test_signal_runner.py::TestHoldTimeout` — disabled-by-config / dry-run / paper-disabled / zero-max-days / recent-BUY-blocks-timeout / stale-BUY-triggers-close-and-persist / no-BUY-history-skipped / shorts-and-flats-filtered. Full suite: **231 passed + 1 skipped** (was 223 + 1 baseline). Verification pending: (a) flip `hold_timeout_enabled=True` in `config/settings.yaml` (Page 5 Risk tab) — the next daily run prints `=== Phase 3.6: Hold timeout ===` and reports `Hold timeouts: 0` in Phase 5 if no positions are stale; (b) a deliberate stale-hold test — set `max_hold_days=1` against a long with a >1-day-old last BUY in `signal_log`, confirm `order_decisions.decision='CLOSED_TIMEOUT'` row appears and IBKR position is flattened; (c) verify the bracket-children cancellation path doesn't strand orphan TRAIL / STP legs (grep daily log for the `Cancelled bracket child` lines that precede the market close).
- *(retired 2026-05-08 — see CHANGELOG.md: "Universe rescore can orphan held positions" + "`signal_log` not populated by daily runner")*

**Performance / hygiene — lower priority:**
- **`reconcile_flex.py` crashes on network/transport errors instead of degrading gracefully** *(code complete 2026-06-15 — awaiting live verification)* (`scripts/reconcile_flex.py:76`, `data/flex_client.py:_default_http_get`): the Step 3c fetch only wraps `fetch_flex_statement` in `except FlexError`, but `_default_http_get` raises raw `urllib.error.URLError` / `socket.gaierror` on a DNS or transport failure — these bypass the handler and propagate as an uncaught traceback, exiting non-zero. This violates the module's own documented contract ("a Flex service error also logs + exits 0 — same graceful-degradation contract as the Gateway phases; tomorrow's run picks up anything missed"). Observed 2026-06-15: the 10:03 ET `SendRequest` hit `[Errno 11001] getaddrinfo failed` (transient DNS blip — every other host that run resolved fine) and dumped a 60-line traceback into the daily log; `run_daily.bat` Step 3c caught the non-zero exit and continued (`WARNING: reconcile_flex.py returned non-zero -- live fills may lag until tomorrow`), so no downstream harm that day (Phase 1 `fetched 0` fills anyway), but the in-script exit-0 path never ran. The risk is that a real between-run fill coinciding with a network hiccup relies on the batch wrapper rather than the intended graceful path. **Fix:** in `data/flex_client.py:_default_http_get`, catch `urllib.error.URLError` / `OSError` and re-raise as `FlexError` so all transport failures funnel through the existing graceful handler (cleaner — keeps `FlexError` the single failure type callers must handle), OR broaden the `except FlexError` in `reconcile_flex.py:main()` to also catch `(URLError, OSError)` with the same log-and-`return 0`. **Status:** implemented the first (cleaner) option — `_default_http_get` now wraps the `urllib.request.urlopen` call in `try/except (urllib.error.URLError, OSError)` and re-raises as `FlexError(f"Flex HTTP request failed: {exc}")`, so every transport/DNS failure funnels through the single `FlexError` type that `reconcile_flex.py:main()` already catches and exits 0 on. No change to `reconcile_flex.py` itself (its existing `except FlexError` handler now covers the transport case too). Added `import urllib.error` to `data/flex_client.py`. Test coverage: 1 new in `tests/test_flex_client.py::test_default_http_get_reraises_transport_error_as_flexerror` (monkeypatches `urlopen` to raise `URLError`, asserts `FlexError` with the "Flex HTTP request failed" message). Full `test_flex_client.py`: 13 passed (was 12). Verification pending: the next daily run where Step 3c's HTTP fetch hits a transient DNS/transport failure should log `WARNING Flex fetch failed — skipping reconciliation this run: Flex HTTP request failed: …` and print the `⚠ Flex fetch failed` / `Skipping (exit 0)` lines, then exit 0 with NO traceback in the daily log (grep the daily log for `Traceback` near a Step 3c failure — should be absent).
- **Row-by-row upserts** *(code complete 2026-06-16 — `upsert_bars` / `upsert_indicators` done; `upsert_news` out of scope)* in `data/database.py`: `upsert_bars` / `upsert_indicators` ran one `SELECT` per DataFrame row inside the session loop — a 365-bar × ~68-symbol backfill was ~25k round-trips. **Fix:** both helpers now issue a **single range query** (`timestamp >= min(rows) AND timestamp <= max(rows)`) to load all existing rows for the (symbol, interval) up front into a dict, then partition the df into inserts (`add_all`) vs in-place updates (overwrite path only) — collapsing N `SELECT`s into 1. DataFrame rows are deduped on normalised timestamp (dict key, last-wins) which also removes the prior latent risk of two same-timestamp inserts hitting the UNIQUE constraint in one batch. The range-query form (vs `timestamp.in_(...)`) avoids SQLite's bound-parameter limit on wide backfills. Exact skip/overwrite semantics preserved (default skips existing, `overwrite=True` updates in place; return value still counts inserted-OR-updated). `upsert_news` was **deliberately left unchanged** — it's single-article by signature (one row per call, returns a per-article bool), so the df-loop round-trip problem doesn't apply; bulking it would mean changing its API and every caller for marginal gain. **Test coverage:** existing `TestUpsertBarsOverwrite` (×3) + `TestUpsertIndicatorsOverwrite` (×2) in `tests/test_data_pipeline.py` already pin the skip/overwrite/mixed-batch semantics and pass unchanged; full suite **467 passed**. Verification pending: this is invisible behaviourally (same rows written, faster) — the observable is a shorter Phase-2 / EOD-refresh wall-clock on the next `run_pipeline.py` / `run_eod.bat` run; no correctness signal to wait on.
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
  **Verification update (2026-05-31 — PARTIAL; spot-check FAILS, residual leak found):** the spot-check still
  returns an ERROR line for 10148. Root cause: there are **two** log lines per 10148 event, from two different
  loggers. (1) *Our* handler line (`trading.execution.ibkr` → `IBKR error [10148]`) is now correctly routed to
  `log.debug` and, since `trading.*` defaults to INFO, is suppressed entirely — last seen at ERROR on 2026-05-19,
  the day the fix landed. ✅ That half works. (2) **ib_insync's own `ib_insync.wrapper` logger emits its own
  `ERROR Error 10148, reqId N: …` line** *before* dispatching the `errorEvent` our `_on_error` listens to — this
  line is entirely outside the `informational`-set routing and is captured into the daily batch log via the
  root-logger / stderr path. Confirmed present at ERROR in `logs/daily/daily_run_20260526.log` (`ib_insync.wrapper
  ERROR Error 10148, reqId 149: …`). So the original fix de-noised our duplicate but not the primary noise source.
  **Residual fix options:** (a) attach a `logging.Filter` to the `ib_insync.wrapper` logger that drops records whose
  message contains a code in our informational set (most targeted; survives ib_insync version bumps as long as the
  message format holds); (b) raise the `ib_insync.wrapper` logger level to CRITICAL and rely solely on our own
  `_on_error` routing for all IBKR error visibility (simpler, but loses ib_insync's own line for *real* errors too);
  (c) accept the residual ERROR line as cosmetic (it is genuinely benign) and just correct the spot-check wording in
  this entry. Option (a) is the right call if log-noise reduction is the goal; (c) if it isn't worth the surface.
  Note the same residual applies to **every** code in the `informational` set, not just 10148 — any of them that
  ib_insync classifies as an error (10148, 202, 10349, …) will produce an `ib_insync.wrapper ERROR` line our routing
  can't touch. A filter (option a) would fix the whole class at once.
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
size. **Status:** implemented as `scripts/refresh_recent_bars.py` + a new `run_eod.bat` scheduler wrapper. The spec's "overwriting on upsert is already the behavior" turned out to be wrong — `upsert_bars` / `upsert_indicators` both skipped existing rows. Two additions: (a) new `overwrite: bool = False` parameter on both helpers in `data/database.py` (default preserves existing skip-existing semantics for the incremental-fetch path); (b) new `DataFetcher.refresh_recent(symbol, interval='1d', days_back=5)` that calls yfinance and upserts with `overwrite=True`. The script builds its symbol union from three sources: (1) `get_universe_assets(active_only=True)` or `config.data.watchlist`, (2) `order_decisions` with `decision in ('APPROVED', 'DRY_RUN', 'CLOSED_LONG')` and `decided_at > now − 14d` (as a Phase-A proxy for "recently-held" until Phase B lands `source='live'` rows in `trade_log`), (3) currently-held IBKR positions (optional — `--no-ibkr` flag, also degrades cleanly to empty set when Gateway is unreachable). For each symbol it overwrites recent bars then recomputes and overwrites the derived indicators. `run_eod.bat` lives separately from `run_daily.bat` because the daily run fires pre-market at 09:40 ET; EOD wrapper logs to `logs/eod/eod_run_YYYYMMDD.log` and must be scheduled separately via Windows Task Scheduler at 16:30 ET. Test coverage added: 5 new in `tests/test_data_pipeline.py` (`TestUpsertBarsOverwrite` × 3: default skips, overwrite updates in place with the low-overwrite canary, mixed batch handles INSERT+UPDATE in one call; `TestUpsertIndicatorsOverwrite` × 2). Full suite: **223 passed + 1 skipped**. Verification pending: (a) ~~the Phase B `source='live'` lookup in `_recently_acted_symbols` should be switched from `order_decisions` to `trade_log` once Phase B accumulates rows~~ **DONE 2026-06-04** — rather than switch `_recently_acted_symbols`, added a *separate* `_recently_exited_symbols(days=14)` that reads `trade_log` `source='live'` exits (bracket TP/stop/trailing exits leave no `order_decisions` row, so they need the trade_log path) and unioned it in; covered by `tests/test_refresh_recent_bars.py` (4 tests). Motivated by the SNOW gap (exit 2026-05-21, last cached bar 2026-05-20); (b) needs a real EOD cron run to confirm yfinance returns finalised post-close bars at 16:30 ET (yfinance typically updates ~15-20 min after close) and that the held-positions union actually catches the rotated-out symbols that motivated this fix; (c) repeat the TMUS / UAL / TEL spot-check from the original observation against the DB after one EOD run completes — DB Low values should match yfinance Low values to within a cent.

### Enhancements (open)

Full write-ups relocated to `docs/enhancements.md` (2026-06-17) to keep this file under its size limit. Pointers below carry the name, status, and trigger-to-revisit; open the doc for design detail, rationale, and implementation status.

**Ideas — not started (measure premise first):**
- **Macro/geopolitical headline overlay** — market/sector regime dial from broad-tape headlines; measure premise with the score-vs-forward-return harness (commit 7f838eb) before building. Parked 2026-06-04.
- **LLM-news-as-threshold-dial** — drop FinBERT from the ensemble; use the LLM-news composite as a BUY/SELL *threshold dial*, not a score component. Trigger: ≥50 matched `source='live'` BUYs with non-`cb_flatten` exits in the LLM-coverage era (testable n≈2 today). Operator proposal 2026-06-12.
- **Richer cost model** — bid-ask spread, partial fills, market impact in `models/walk_forward.py`.

**In-flight / iterating:**
- **LLM news analyst (Page 11)** *(core shipped + running daily since 2026-06-02; iterating)* — 8-item open list (event-score MEAN→max-magnitude, prompt sharpening to score the primary company, `mentioned_tickers`, representative-selection, body-level dedup, etc.).
- **Phase 4.5 — Realised P&L plumbing** — Phase A verified + Phase B shipped (both in CHANGELOG); Phase C (realised-Kelly) implemented, Sunday-retrain verification pending. Full schema/spec + per-bar bracket order-of-operations retained in the doc.

**Code complete — awaiting live verification:**
- **Benchmark-relative performance tracking (Page 10)** — code complete 2026-05-19; verifies on the next weekly retrain (baseline-pin canary tests fire by design).
- **Intraday lightweight runner (Phase 1 + 3.5)** — `scripts/intraday_check.py` shipped 2026-05-20; awaiting the first Task-Scheduler 12:00/15:30 ET runs + a deliberate Gateway-down test.
- **Expand `_SECTOR_MAP`** — code complete 2026-05-12; *superseded 2026-06-03 by data-driven sector classification* (see arch note). Observable: non-zero `sector_exposure` rejections reach the previously-silent half of the universe.
- **Pervasive SELL bias** — Option A+C mitigation code complete 2026-05-11; *priority downgraded 2026-05-13 to observe-don't-act* (the bias may be alpha, not artefact). Option B (lengthen XGBoost `_FORWARD_BARS` 5→20) deferred until ≥2 weeks of Phase B realised P&L decide it.

**Deferred designs (trigger-gated):**
- **Trailing stop crowded out by bracket TP** — *refined 2026-06-16 (`docs/findings/trail_vs_tp_capture.md`)*: now a ~net-neutral path-shape bet, not one-sided loss. Don't retune defaults; waits on the finding's H3 parameter grid + ≥10 live exits per bucket.
- **LSTM↔MACD direction-disagreement gate** — block BUY when LSTM>0.7 AND MACD<0 (TEL archetype). Defer until ≥10 `source='live'` rows cross-tab MACD-at-entry vs outcome.
- **LSTM-saturated-bearish held longs** — conviction override / cap FinBERT upside when LSTM≤−0.95 props a held long above the SELL gate (AZN archetype). Defer until Phase B can compare realised exits vs SELL-at-saturation.
- **Point-in-time fundamentals for WF** — yfinance quarterly statements vs current snapshot (mild lookahead in XGBoost features); options ranked by effort in the doc.
- **Consider WF Sharpe in signal generation** — hard filter vs soft penalty on low-Sharpe symbols; defer until ≥2 weeks of Phase B rows (filter on realised P&L, not WF Sharpe alone).
- **Wire up `reqPnL` for intraday CB triggering** — gate CLEARED (CB verified live 2026-06-10); remaining win is re-checking loss limits *between phases within a single run* via a streaming subscription.
- **Test IBKR scanners as a Stage 1 universe replacement** — removes the Alpaca dependency; defer until ≥2 weeks of Phase B `source='live'` rows (compare universes on realised P&L).
- **Adopt IBC for unattended IB Gateway** — auto-login/restart so morning runs don't silently fall back to dry-run; no longer a Phase B prerequisite (nice-to-have for live-order timeliness).
- **Track IBKR/Alpaca/yfinance news source hit-rate over time** — observability (Page 6 source-mix donut + timeout sparkline); defer until IBKR timeout rate creeps above ~70%.

**Logging / observability:**
- **Weekly log trim** — demote WF per-fold framework chatter to DEBUG; trigger: first Sunday weekly run still >12k lines after the daily-loop trim.
- **Logging quality v2** — structured `GuardResult.checks` on REJECT (+`failed_check` column), Kelly inputs at INFO, Phase-5 reject histogram, `run_id` on every log line.
- **Automate post-daily-run log review** — wire `/daily-run-review` to a schedule; trigger: Phase B stable ~3 wks + ≥10 manual runs + attention drifting off the app.

**Completed (breadcrumb):**
- **Retire verified-but-unmarked bug fixes** *(completed 2026-05-11)* — datetime.utcnow / dashboard path / LSTM determinism retired to CHANGELOG.

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
