# System Overview

## What this system does

At its core, this is a **daily signal generator**: every morning before market open it reads price history, runs three machine learning models, combines their outputs into a single score, filters that score through a risk layer, and produces a list of BUY/SELL/HOLD decisions — one per symbol.

It does not trade autonomously by default. The dry-run mode (default) logs every decision without submitting any orders, so you can observe what it would have done before trusting it with real capital.

---

## Architecture at a glance

```
External data sources
  yfinance ──────────────────────────────┐
  IBKR / Alpaca (news) ──────────────────┤
                                         ▼
                               ┌─────────────────┐
                               │  Data Pipeline  │  run_pipeline.py
                               │  (SQLite store) │
                               └────────┬────────┘
                                        │
                               ┌────────▼────────┐
                               │  Model Training │  train_models.py
                               │  (walk-forward) │
                               └────────┬────────┘
                                        │
                               ┌────────▼────────┐
                               │  Signal Runner  │  signal_runner.py
                               │  LSTM           │
                               │  XGBoost        │
                               │  FinBERT        │
                               │  ── Ensemble ── │
                               │  Signal Gate    │
                               │  Risk Layer     │
                               └────────┬────────┘
                                        │
                        ┌───────────────┼───────────────┐
                        ▼               ▼               ▼
                   SQLite DB      IB Gateway        Dashboard
                  (decisions)    (orders, live)    (Streamlit)
```

---

## The seven phases of a daily run

The `signal_runner.py` script works in seven sequential phases (Phase 3.5 and Phase 3.6 are both opt-in):

### Phase 1 — Startup
First (when not in dry-run) reconcile any off-cycle IBKR fills into `fill_log` + `trade_log` (Phase B — `execution/reconciliation.py`), so exits that filled between runs are captured before sizing reads `trade_log`. Then determine which symbols to process, check the circuit breaker state, capture an equity-baseline snapshot from IBKR, and auto-trip the circuit breaker if the realised + unrealised P&L vs that baseline has exceeded the daily / weekly loss limits. Includes orphan-position detection so any held long not in the active universe still gets pulled into the symbol list.

> **Note — `reqExecutions` is current-session only.** The in-run Phase 1 poll uses IBKR `reqExecutions`, which only returns the *current* Gateway session's fills. On a Gateway that resets overnight (this account's normal behaviour) the morning poll typically sees nothing from the prior session, so between-run fills are missed by the live path. The durable backstop is the **IBKR Flex Query Web Service** (`scripts/reconcile_flex.py`, run as daily-batch Step 3c), which is session-independent and retains a year+ — it recovers prior-day fills the morning after (Flex is T+1). The two are complementary: `reqExecutions` catches same-session fills, Flex catches everything through yesterday. See `data/flex_client.py` and the CLAUDE.md "Flex Web Service is the durable trade-history source" architectural-decision note.

### Phase 2 — Data refresh
Fetch the latest OHLCV bars and news for the symbols selected in Phase 1. This is an incremental update — only new bars since the last run are fetched. yfinance is the primary source for price data; IBKR/Alpaca/yfinance are tried in order for news. Symbols whose newest cached daily bar is older than `risk.max_bar_staleness_days` (default 3) are dropped before Phase 3 generates signals.

### Phase 3 — Signal generation
For each symbol, load the saved model checkpoints and run inference:
- **LSTM** reads the last 60 bars of price + indicator data
- **XGBoost** reads current indicator values + fundamental ratios
- **FinBERT** aggregates recent news headlines into a sentiment score
- **Ensemble** combines the three scores using weighted averaging (XGBoost weight is halved in TRENDING regime)

The signal gate then runs three sequential filters — threshold, regime-adjusted threshold, and model confirmation (≥ 2 of 3 agree). Only signals that pass all three become BUY or SELL decisions; the rest fall to HOLD.

### Phase 3.5 — Trailing-stop conversion (opt-in)
When `risk.trailing_stop_enabled=True` AND `trading.paper_orders_enabled=True` AND NOT dry-run, the `TrailingStopManager` walks every open long position. For each position that has moved ≥ `trailing_stop_activation_atr × ATR` into profit, the bracket's TP and STP legs are cancelled and replaced by a single standalone GTC `TRAIL` order. Idempotent — positions with an existing TRAIL order are skipped.

### Phase 3.6 — Hold-timeout flatten (opt-in)
When `risk.hold_timeout_enabled=True` AND `trading.paper_orders_enabled=True` AND NOT dry-run AND `max_hold_days > 0`, each held long is checked against `signal_log` for its most recent passed-gate BUY. If that BUY is older than `max_hold_days` (default 30 calendar days), the position is flattened with a market sell after cancelling its bracket children (LMT TP / STP / STP LMT / TRAIL). Positions with no BUY history in `signal_log` are skipped — manual positions / pre-history holdings have no staleness anchor. The "re-confirming signal" semantic preserves winners the model still actively likes; only positions the model has ignored for a full month are flattened. Each closure persists to `order_decisions` with `decision='CLOSED_TIMEOUT'`.

### Phase 4 — Risk & order decisions
Each signal passes through the portfolio guard (seven checks) and position sizer (Kelly criterion + ATR). The result is an `OrderDecision` with entry price, stop loss, and take profit levels. SELL signals against existing longs flatten the position (`CLOSED_LONG`); SELL signals from flat without `allow_short_selling` are rejected (`REJECTED_NO_POSITION`).

### Phase 5 — Summary
Write a `signal_runner_log` row with run-level counters (signals generated, orders submitted, rejected, longs closed, trailing conversions, hold-timeout flattens, skipped duplicates, stale-bar drops) and print a summary table. Order submission itself happens inside Phase 4 — in dry-run mode (default) decisions are written to `order_decisions` only; in paper mode bracket orders are submitted to IBKR; in live mode they go to the live account.

---

## Complementary runners (intraday and post-close)

The seven-phase daily runner above fires once per weekday at 09:40 ET via `run_daily.bat`. Two smaller runners handle work that has to happen on a different cadence:

### Intraday lightweight runner — `scripts/intraday_check.py`
Scheduled at 12:00 ET and 15:30 ET via `run_intraday.bat`. Runs only Phase 1 (circuit-breaker check against live IBKR account P&L) and Phase 3.5 (trailing-stop re-evaluation against live `IBKRConnection.get_last_price()`, NOT the cached daily bar). Does **not** regenerate signals, refresh data, fetch news, retrain models, rescore the universe, or evaluate hold-timeouts — those stay on the daily/weekly cadence. Each invocation writes one row to `intraday_run_log` (status ∈ `completed` / `gateway_down` / `cb_tripped` / `error`). Ratchet-only by default; new TP→TRAIL conversions are opt-in via `RiskConfig.intraday_trail_conversion_enabled`. Exits 0 on Gateway-down to avoid Task Scheduler retry storms — a missed slot surfaces as a `status='gateway_down'` row on Page 8 instead.

### End-of-day bar refresh — `scripts/refresh_recent_bars.py`
Scheduled at 16:30 ET via `run_eod.bat`. The morning Phase 2 fetches today's daily bar pre-market, which is a partial intraday snapshot. Symbols that subsequently drop out of the active universe AND aren't held never have that partial bar corrected — its recorded daily high/low can sit wrong forever, hiding intraday stop-outs from the dashboard and biasing walk-forward retraining. The EOD refresh overwrites the last 5 days of bars + indicators for the union of (active universe, recently-acted symbols via `order_decisions` last 14 days, currently-held IBKR positions) using `upsert_bars(..., overwrite=True)`. Tolerates IB Gateway being down via `--no-ibkr`.

---

## Key design decisions

### Why SQLite instead of PostgreSQL?
This is a single-user learning tool. SQLite requires no server, lives in a single file, and is fast enough for hundreds of symbols. All timestamps are stored as UTC-naive datetimes to avoid SQLAlchemy timezone complexities.

### Why yfinance for data?
Free, no API key required, covers equities worldwide, returns adjusted OHLCV going back decades. The tradeoff is rate limits and occasional data gaps — the pipeline is designed to be idempotent (safe to re-run) and incremental (only fetches new bars).

### Why three models instead of one?
Each model captures a different signal:
- **LSTM** learns temporal patterns in price (momentum, mean reversion)
- **XGBoost** combines technical + fundamental features (valuation, quality)
- **FinBERT** captures sentiment not visible in price data at all

No single model dominates all market conditions. The ensemble blends them, and the walk-forward rebalancer shifts weight toward whichever model has been more accurate recently.

### Why walk-forward validation?
Financial time series can't be shuffled — the future can't be used to predict the past. Walk-forward validation enforces this: training always precedes testing in time. See [Walk-Forward Validation](03-walk-forward.md) for details.

### Why dry-run by default?
Two gates protect against accidental order submission:
1. `signal_runner.py` defaults to `--dry-run`
2. `trading.paper_orders_enabled` must be `True` in config

Both must be cleared to place orders. This prevents expensive mistakes during development.

---

## Data flow in detail

```
yfinance.Ticker(symbol).history()
    │
    ▼
DataFetcher.fetch_symbol()
    │   incremental upsert into ohlcv_bars
    ▼
IndicatorEngine.run()
    │   RSI, MACD, Bollinger Bands, EMA, ATR, Volume SMA
    │   upsert into indicator_snapshots
    ▼
NewsClient.fetch_news()
    │   IBKR → Alpaca → yfinance fallback
    │   upsert into news_cache
    ▼
FundamentalsClient.get()
    │   P/E, forward P/E, margins, growth rates (24h cache)
    │   upsert into fundamental_data
    ▼
MLWalkForwardOrchestrator.predict()
    │   loads saved LSTM + XGBoost checkpoints
    │   scores each bar with FinBERT(as_of=bar_timestamp)
    │   applies ensemble weights
    │   runs signal gate
    │   writes to signal_log
    ▼
TrailingStopManager.manage()          ← Phase 3.5, runs before new orders
    │   walks open longs, converts qualifying TPs to trailing stops
    │   writes to trailing_stop_log
    ▼
OrderManager.process()
    │   PositionSizer (Kelly + ATR — realised-Kelly once trade_log has ≥30 closed trades)
    │   PortfolioGuard (7 sequential checks)
    │   writes to order_decisions
    ▼
IBKRConnection.place_bracket_order()   ← only when paper_orders_enabled=True
    │                                    (entry LMT + stop STP + take-profit LMT, all GTC)
    ▼
walk_forward bracket simulator        ← during model training
    │   simulates the same execution path bar-by-bar
    │   writes closed trades to trade_log (source='walk_forward')
```

---

## The database (SQLite)

Nineteen tables in `db/trading.db`. You never need to interact with it directly — the dashboard reads from it, and all writes go through the ORM helper functions in `data/database.py`.

The most important tables:

| Table | Written by | Read by |
|-------|-----------|---------|
| `ohlcv_bars` | DataFetcher | LSTM, XGBoost, RegimeDetector |
| `indicator_snapshots` | IndicatorEngine | XGBoost, dashboard |
| `news_cache` | NewsClient | FinBERT |
| `fundamental_data` | FundamentalsClient | XGBoost |
| `signal_log` | Orchestrator | Dashboard page 3 |
| `order_decisions` | OrderManager | Dashboard page 8 |
| `trade_log` | Walk-forward simulator (`source='walk_forward'`) + IBKR fill reconciliation (`source='live'`, Phase B — `execution/reconciliation.py`) | Dashboard page 10, realised-Kelly |
| `fill_log` / `reconciliation_state` | IBKR fill reconciliation (Phase B — `reqExecutions` in-session poll **and** the Flex Web Service daily backstop, `scripts/reconcile_flex.py`) | `trade_log` aggregation (audit trail + watermark) |
| `trailing_stop_log` | TrailingStopManager | Dashboard page 8 |
| `signal_runner_log` | signal_runner.py | Dashboard page 8 |

Schema migrations are handled by `_migrate()` in `data/database.py` — it runs at every engine init and adds new columns with idempotent `ALTER TABLE` statements. See [CLAUDE.md](../CLAUDE.md) for the full 19-table schema reference. The tables not shown above: `equity_snapshots` (circuit-breaker baseline), `intraday_run_log` (one row per `intraday_check.py` invocation), `ensemble_weight_history` / `walk_forward_results` / `universe_assets` / `universe_run_log` / `circuit_breaker_log` / `fundamental_data`, and `llm_news_analysis` (the LLM news analyst's per-article extractions — see below).

### A parallel research lane — the LLM news analyst

Separate from the seven-phase trading path, a **shadow workflow** reads full news article bodies through a local LLM and writes structured sentiment to `llm_news_analysis`. **Nothing in `signal_runner.py` reads it** — it is surfaced only on dashboard Page 11. It runs on its own batch cadence (`run_llm_news.bat`), off the pre-market critical path, and is gated behind `config.llm.enabled` (default off). See [LLM News Analyst](11-llm-news-analyst.md) for the full design.
