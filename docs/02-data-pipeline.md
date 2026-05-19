# Data Pipeline

## Overview

The data pipeline has one job: take raw price and news data from external sources and store it in a clean, consistent SQLite database that the ML models and dashboard can query. It runs before every model training or signal generation session.

```
run_pipeline.py
  ├── VIX cache          (^VIX daily bars via yfinance)
  ├── OHLCV fetch        (yfinance → ohlcv_bars)
  ├── Indicator compute  (IndicatorEngine → indicator_snapshots)
  ├── News fetch         (IBKR → Alpaca → yfinance → news_cache)
  └── FinBERT scoring    (ProsusAI/finbert → news_cache.sentiment_score)
```

---

## Price data: yfinance

[yfinance](https://github.com/ranaroussi/yfinance) is an unofficial Python wrapper around Yahoo Finance's API. It provides:
- **Daily OHLCV** going back decades (adjusted for splits and dividends)
- **Intraday** data: 1h bars (~59 days back), 30m/15m/5m (~60 days)
- **Metadata**: market cap, sector, analyst targets, earnings dates

### How fetching works

```python
import yfinance as yf
ticker = yf.Ticker("AAPL")
df = ticker.history(period="1y", interval="1d", auto_adjust=True)
```

`auto_adjust=True` applies split and dividend adjustments automatically, so the Close column represents the true economic return rather than the raw traded price.

### Incremental updates

The pipeline is **idempotent** — safe to run multiple times. `DataFetcher.fetch_symbol()` checks the latest bar timestamp in SQLite and fetches only bars newer than that date. On first run it fetches `daily_lookback_days` (default 365) of history; subsequent runs fetch just the new bars since the last run.

```python
# Only fetch what we don't have yet
latest_stored = get_latest_bar_timestamp(symbol, interval)
df = yf.Ticker(symbol).history(start=latest_stored, interval=interval)
upsert_bars(symbol, interval, df)   # default: INSERT-or-SKIP (existing rows untouched)
```

`upsert_bars` skips rows that already exist by default — this is what makes repeated runs cheap. There is also an `overwrite=True` mode that UPDATEs the OHLCV columns in place; the end-of-day refresh (below) uses it to replace mid-day partial bars with their final post-close values.

This makes the pipeline fast on daily runs (~1 second per symbol after the initial seed).

### End-of-day bar refresh

The morning `signal_runner.py` Phase 2 fetches each symbol's daily bar mid-day (~09:35–10:00 ET), so what yfinance returns for *today's* bar is a partial intra-day snapshot — not the final daily Open/High/Low/Close. The previous day's bar (D-1) is already finalised by then, but only if the morning run actually re-fetches it; once a partial bar is in SQLite, the default `upsert_bars` path will skip it on every subsequent call.

The symptom is silent and asymmetric: held positions get their bars refreshed indirectly through downstream code paths, but **symbols that drop out of the active universe AND are no longer held never have their stale bar corrected**. The recorded daily Low can sit above the day's true Low forever — which hides intraday stop-outs from the dashboards, misleads case-study analysis, and biases walk-forward retraining (the model trains on a price history that doesn't match what actually happened).

To close the gap, `scripts/refresh_recent_bars.py` runs once per weekday at 04:30 PM ET (via `run_eod.bat` in Windows Task Scheduler) and re-fetches the last 5 days of bars for the union of:

1. **Active universe** — `universe_assets WHERE active=1` (or `config.data.watchlist` when universe is disabled)
2. **Recently-acted symbols** — `order_decisions` rows with decision in (`APPROVED`, `DRY_RUN`, `CLOSED_LONG`) and `decided_at` within the last 14 days (proxy for "recently-held" until Phase 4.5 Phase B populates `trade_log.live` rows)
3. **Currently-held IBKR positions** — optional; degrades cleanly when IB Gateway is unreachable (`--no-ibkr` flag, or auto-degrades on connect failure)

For each symbol, the script calls `DataFetcher.refresh_recent()` (which writes via `upsert_bars(... overwrite=True)`) then recomputes the derived indicators from the refreshed bars and writes them via `upsert_indicators(... overwrite=True)`. The whole pass runs in ~5–10 seconds for the current universe.

```bash
python scripts/refresh_recent_bars.py              # default: last 5 days
python scripts/refresh_recent_bars.py --days 10    # wider backfill window
python scripts/refresh_recent_bars.py --no-ibkr    # skip the IBKR positions union
```

---

## Technical indicators

Technical indicators are computed by `IndicatorEngine` using the [pandas-ta](https://github.com/twopirllc/pandas-ta) library. They are stored separately in `indicator_snapshots` (not in `ohlcv_bars`) so the raw price history is never modified.

### Indicators computed

| Indicator | What it measures | Formula |
|-----------|-----------------|---------|
| **RSI-14** | Overbought/oversold momentum | `100 - 100/(1 + avg_gain/avg_loss)` over 14 bars |
| **MACD** | Trend momentum and direction | EMA(12) - EMA(26); signal = EMA(9) of MACD |
| **MACD Signal** | Smoothed MACD for crossover detection | EMA(9) of the MACD line |
| **Bollinger Upper** | Upper price band (2σ above mean) | SMA(20) + 2 × rolling std |
| **Bollinger Lower** | Lower price band (2σ below mean) | SMA(20) - 2 × rolling std |
| **EMA-20** | Short-term trend | Exponentially weighted 20-bar average |
| **EMA-50** | Medium-term trend | Exponentially weighted 50-bar average |
| **ATR-14** | Volatility (average true range) | Mean of `max(H-L, |H-prev_C|, |L-prev_C|)` over 14 bars |
| **Volume SMA-20** | Average trading volume | Simple 20-bar average of volume |

### How to read these indicators

**RSI (0–100):**
- Above 70 → historically overbought, potential reversal down
- Below 30 → historically oversold, potential reversal up
- The LSTM uses the raw RSI value; XGBoost uses it as one of 12 features

**MACD:**
- Positive and rising → bullish momentum
- Crosses above signal line → buy signal in classic technical analysis
- The project uses MACD as an input feature, not as a standalone signal

**Bollinger Bands:**
- Price touching upper band → strong uptrend or overbought
- Price touching lower band → strong downtrend or oversold
- Band width measures volatility (wide = volatile, narrow = consolidating)

**ATR (Average True Range):**
- Measures how much a stock typically moves per bar
- Used by `PositionSizer` to set stop loss and take profit distances
- Higher ATR → wider stops (the stock needs room to breathe)

---

## News data

### Three-tier fallback

News is fetched by `NewsClient` which tries three sources in priority order:

```
1. IBKR / Dow Jones    ← best quality, ~4 months back, 300 article cap
        ↓ (if IB Gateway not running or no articles)
2. Alpaca Markets API  ← broad coverage, configurable lookback
        ↓ (if no API key)
3. yfinance            ← always available, ~10 most recent articles only
```

This tiered approach means the system degrades gracefully without any API keys configured — yfinance is always available as a fallback.

### IBKR news specifics

IBKR's `reqHistoricalNews` API returns Dow Jones Newswires and Briefing.com articles. Headlines come with a Dow Jones prefix that must be stripped before FinBERT scoring:

```python
# Raw headline from IBKR:
"{A:800015:L:en} Apple Reports Record Q4 Earnings"

# After stripping:
"Apple Reports Record Q4 Earnings"
```

The IBKR news API has a hard cap of 300 articles per symbol and goes back approximately 4–5 months.

### Upsert without overwriting scores

A critical design choice: `upsert_news()` **never overwrites an existing sentiment score**. FinBERT scoring is expensive (~0.5 seconds per article, plus 30 seconds to load the model). Once an article is scored, that score is permanent:

```python
# Only fills in sentiment_score when it's currently NULL
INSERT OR REPLACE INTO news_cache (...)
-- but on conflict, only update score if the stored value IS NULL
```

This means `run_pipeline.py` can be re-run safely — only new, unscored articles are processed.

---

## FinBERT sentiment scoring

[FinBERT](https://huggingface.co/ProsusAI/finbert) is a BERT model fine-tuned on financial text. It classifies each headline as **positive**, **negative**, or **neutral** and returns confidence scores.

### Scoring pipeline

```python
from transformers import pipeline

pipe = pipeline("text-classification", model="ProsusAI/finbert")
result = pipe("Apple Reports Record Quarterly Earnings")
# → [{"label": "positive", "score": 0.97}]
```

The raw label and confidence are converted to a score in [-1, 1]:
- `positive` → `+confidence`
- `negative` → `-confidence`
- `neutral` → `0.0`

### Time-decay aggregation

When FinBERT is asked to score a symbol (for signal generation), it doesn't just use the most recent headline — it aggregates all recent articles with exponential time decay:

```
score = Σ(article_score × e^(-λt)) / Σ(e^(-λt))
```

Where `t` is the article's age in hours and `λ` is derived from `sentiment_half_life_hours` (default 24h). Articles older than `sentiment_staleness_days` (default 7) contribute zero weight.

This means a single dramatic headline from yesterday outweighs five mildly positive headlines from last week.

### Lookahead prevention

During walk-forward backtesting, FinBERT is called with `as_of=bar_timestamp`. This filters the news cache to only include articles published **on or before** that bar's date, preventing the model from "seeing" future news during historical evaluation.

---

## VIX caching

The CBOE Volatility Index (^VIX) is fetched alongside equity data and stored in `ohlcv_bars`. It drives the regime detector:
- VIX > 25 → HIGH_VOLATILITY regime → signal gate raises threshold by 1.5×
- VIX ≤ 25 → normal or trending regime

The VIX cache has a 4-hour TTL. When the cache is stale inside a Streamlit session, the cached value is used rather than blocking the UI thread — `run_pipeline.py` refreshes it on every run.

---

## SQLite storage

### Why SQLite?

For a single-user application processing ~50–100 symbols daily, SQLite is more than sufficient:
- No server process to manage
- Single file (`db/trading.db`) — easy to back up, inspect, or reset
- Reads are fast because all queries hit a local file (no network)
- SQLAlchemy ORM provides a clean Python interface

### Schema migration pattern

When a new column is added to a table, the `_migrate()` function in `data/database.py` handles it:

```python
def _migrate(conn):
    cols = [row[1] for row in conn.execute("PRAGMA table_info(news_cache)")]
    if "sentiment_score" not in cols:
        conn.execute("ALTER TABLE news_cache ADD COLUMN sentiment_score REAL")
```

This runs at every engine initialization and is idempotent — it checks before altering, so it's safe to run repeatedly. This avoids the complexity of a full migration framework like Alembic.
