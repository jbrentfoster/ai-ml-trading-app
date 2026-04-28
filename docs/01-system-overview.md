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

## The five phases of a daily run

The `signal_runner.py` script works in five sequential phases:

### Phase 1 — Data refresh
Fetch the latest OHLCV bars and news for all symbols. This is an incremental update — only new bars since the last run are fetched. yfinance is the primary source for price data; IBKR/Alpaca/yfinance are tried in order for news.

### Phase 2 — Signal generation
For each symbol, load the saved model checkpoints and run inference:
- **LSTM** reads the last 60 bars of price + indicator data
- **XGBoost** reads current indicator values + fundamental ratios
- **FinBERT** aggregates recent news headlines into a sentiment score
- **Ensemble** combines the three scores using weighted averaging

### Phase 3 — Signal gate
Filter signals through three sequential checks:
1. Is the ensemble score strong enough? (`|score| >= threshold`)
2. Adjusted for market regime? (high volatility raises the bar)
3. Do at least 2 of 3 models agree on direction?

Only signals that pass all three gates become BUY or SELL decisions.

### Phase 4 — Risk & order decisions
Each signal passes through the portfolio guard (six checks) and position sizer (Kelly criterion + ATR). The result is an `OrderDecision` with entry price, stop loss, and take profit levels.

### Phase 5 — Order submission
In dry-run mode (default): log the decision to `order_decisions` table. In paper mode: submit bracket orders to IBKR paper account. In live mode: submit to live account.

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
OrderManager.evaluate()
    │   PortfolioGuard (6 checks)
    │   PositionSizer (Kelly + ATR)
    │   writes to order_decisions
    ▼
IBKRConnection.place_bracket_order()   ← only when paper_orders_enabled=True
```

---

## The database (SQLite)

Ten tables in `db/trading.db`. You never need to interact with it directly — the dashboard reads from it, and all writes go through the ORM helper functions in `data/database.py`.

The most important tables:

| Table | Written by | Read by |
|-------|-----------|---------|
| `ohlcv_bars` | DataFetcher | LSTM, XGBoost, RegimeDetector |
| `indicator_snapshots` | IndicatorEngine | XGBoost, dashboard |
| `news_cache` | NewsClient | FinBERT |
| `fundamental_data` | FundamentalsClient | XGBoost |
| `signal_log` | Orchestrator | Dashboard page 3 |
| `order_decisions` | OrderManager | Dashboard page 8 |

Schema migrations are handled by `_migrate()` in `data/database.py` — it runs at every engine init and adds new columns with idempotent `ALTER TABLE` statements.
