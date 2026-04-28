# AI Trading App

A Python-based algorithmic trading system that uses an ML ensemble (LSTM + XGBoost + FinBERT) to generate daily trade signals, managed by a full risk layer (Kelly sizing, ATR stops, bracket orders with optional trailing-stop conversion, circuit breaker, portfolio guard). Connects to Interactive Brokers via `ib_insync` for live/paper order execution. Built on a Streamlit dashboard that explains each component visually.

---

## Build phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | IBKR connection (paper/live trading) | Complete |
| 2 | Data pipeline + indicators + Streamlit dashboard | Complete |
| 3 | ML signal generation (LSTM + XGBoost + FinBERT ensemble) | Complete |
| 4 | Risk & portfolio management | Complete |
| 5 | RL optimizer (PPO, Sharpe reward) | Pending |
| 6 | Live trading transition | Pending |

---

## Requirements

- Python 3.11+
- IB Gateway 10.x (recommended) or TWS — for order execution and premium news; data pipeline works without it
- Windows, macOS, or Linux

```bash
pip install -r requirements.txt
```

All commands must be run from the project root directory.

---

## IBKR setup

The system uses **IB Gateway** (recommended) or TWS for order submission and news. The data pipeline (yfinance) and ML signal generation work without any IBKR connection.

### IB Gateway (recommended)

IB Gateway is a headless version of TWS designed for automated systems — more stable for unattended operation, lower memory usage, and no GUI to close accidentally.

Download from: **ibkr.com → Trading → Trading Software → IB Gateway**

Configuration:
```
Log in with your paper trading credentials
Configure → Settings → API → Settings
  ☑ Enable ActiveX and Socket Clients
  Socket port: 4002          ← paper (default)
  ☐ Read-Only API            ← uncheck to allow orders
Configure → Settings → Auto-restart
  ☑ Auto-restart             ← handles the daily 24h session reset
```

### TWS (alternative)

```
File → Global Configuration → API → Settings
  ☑ Enable ActiveX and Socket Clients
  Socket port: 7497          ← TWS paper
  ☐ Read-Only API
```

### Port reference

| Client | Paper port | Live port |
|--------|-----------|----------|
| IB Gateway | 4002 | 4001 |
| TWS | 7497 | 7496 |

### Market data subscriptions

Having a funded IBKR account does not automatically include real-time API market data. Subscriptions are managed separately in the Client Portal:

**Account Management → Settings → Market Data Subscriptions**

Without a real-time subscription, the system falls back automatically:
1. **IBKR live data** — requires real-time API subscription
2. **IBKR 15-min delayed data** — free, no subscription needed
3. **yfinance** — always available, returns most recent close

The data pipeline (OHLCV bars via yfinance) and historical data requests are unaffected by real-time subscription status.

---

## First-time setup sequence

Run these steps once to seed the database before the automated schedule takes over. In normal operation, `run_daily.bat` and `run_weekly.bat` handle everything — see the [Automatic scheduling](#automatic-scheduling-windows) section below.

### Step 1 — Seed market data

```bash
python scripts/universe_scheduler.py --run-now     # populate universe_assets (if universe.enabled=True)
python scripts/run_pipeline.py                     # fetch OHLCV + news + FinBERT scores for all symbols
python scripts/run_pipeline.py --interval 1h       # also fetch hourly bars (optional)
python scripts/run_pipeline.py --skip-news         # skip news fetch (faster, for debugging)
python scripts/run_pipeline.py --use-watchlist     # force static watchlist even when universe is enabled
```

### Step 2 — Train models

```bash
python scripts/train_models.py                     # train all symbols (full mode, ~2–5 min/symbol)
python scripts/train_models.py --symbol AAPL       # single symbol
python scripts/train_models.py --force             # retrain even if checkpoints already exist
python scripts/train_models.py --quick             # reduced epochs/folds — debugging only, not for production
```

### Step 3 — Generate signals (manual / ad-hoc)

```bash
python scripts/signal_runner.py                    # dry-run, all symbols (default, safe)
python scripts/signal_runner.py --symbol AAPL      # single symbol
python scripts/signal_runner.py --no-dry-run       # submit live paper orders
```

`--no-dry-run` submits **bracket orders** (entry LMT + take-profit LMT + stop STP, all three linked) directly to IB Gateway. Two gates must both be satisfied:

1. IB Gateway is running on the configured port (4002 paper by default)
2. `trading.paper_orders_enabled=True` in `config/settings.yaml`

Each bracket is submitted **GTC** (Good-Till-Cancelled) so orders placed outside regular trading hours survive until the next open. Prices are rounded to $0.01 to avoid IBKR's minimum-tick-variation rejection (error 110). A BUY signal for a symbol already held is blocked by the PortfolioGuard duplicate check.

When `risk.trailing_stop_enabled=True`, a **Phase 3.5 trailing-stop conversion** runs before new-order submission: for each open long position that has moved at least `trailing_stop_activation_atr × ATR` into profit, the bracket's take-profit and stop legs are cancelled and replaced by a single standalone GTC `TRAIL` order. The trailing stop ratchets up with price and triggers only on reversal, letting winners run past the original take-profit. See [docs/08-risk-management.md](docs/08-risk-management.md#trailing-stops-phase-35--opt-in) for the full explanation.

> In production, `signal_runner.py` is called automatically as the final step of `run_daily.bat`. Do not schedule it separately.

### Step 4 — View results

```bash
streamlit run dashboard/1_Market_Data.py
```

---

## Dashboard pages

| Page | Description |
|------|-------------|
| **1 — Market Data** | Candlestick + RSI/MACD/ATR charts, Bollinger Bands, OHLCV table |
| **2 — Fundamentals & News** | P/E, EV/EBITDA, growth metrics, FinBERT sentiment trend, news feed |
| **3 — Model Signals** | Ensemble scores, signal log, XGBoost feature importance, LSTM analysis |
| **4 — Walk-Forward** | Fold Sharpe/drawdown charts, ensemble weight history, results table |
| **5 — Settings** | Full YAML config editor: data, universe, trading, ML, news, IBKR, logging |
| **6 — Data Status** | One row per symbol — bar counts, news coverage by source, model status |
| **7 — Universe** | Funnel overview, active candidates, size history, manual refresh controls |
| **8 — Risk & Portfolio** | Circuit breaker, signal runner log, order decisions + position summary, risk config |
| **Account** | Live IBKR account summary, positions, orders (requires active IB Gateway/TWS) |

---

## Automatic scheduling (Windows)

The daily and weekly runs are driven by two batch files that execute the pipeline steps sequentially. Each step must succeed before the next begins.

### Batch files

| File | When | Steps |
|------|------|-------|
| `run_daily.bat` | Mon–Sat 09:40 AM | `run_pipeline.py` → `universe_scheduler.py --rescore-now --no-signal-run` → `train_models.py` (new symbols only) → `signal_runner.py` (data refresh → signals → trailing-stop conversion → new orders) |
| `run_weekly.bat` | Sunday 01:00 AM | `run_pipeline.py` → `train_models.py --force` → `universe_scheduler.py --run-now` |

**Step ordering matters.** The daily run rescores the universe *before* training so that symbols freshly promoted into the active set get checkpoints the same day rather than next. `--no-signal-run` suppresses the inline signal runner that `universe_scheduler.py` would otherwise fire post-rescore; signals are run explicitly as the final step, after training has caught up.

**Why two separate scripts instead of one persistent scheduler?**
Each batch file runs its steps sequentially and exits. Windows Task Scheduler handles the timing. This avoids silent failures from persistent processes dying overnight, multiple instances on re-login, and race conditions between pipeline and model training.

**Model training cadence:** `run_daily.bat` calls `train_models.py` without `--force`, so it only trains symbols that are missing checkpoints (effectively a no-op once initial training is done, except for newly-promoted universe members). Full retraining happens weekly on Sunday with `--force`. Weekly retraining is sufficient — daily bars change slowly enough that 6-day-old weights remain effective, and the signal gate and FinBERT adapt continuously without retraining.

### Log files

All output is captured to date-stamped log files:

```
logs/
  daily/
    daily_run_20260416.log     ← one file per run
    daily_run_20260417.log
  weekly/
    weekly_run_20260420.log
  python/
    trading_app.log            ← rotating Python logger (all app code)
```

To watch a run in progress:
```powershell
Get-Content logs\daily\daily_run_20260416.log -Wait
```

### Laptop power settings

For unattended overnight operation on a laptop:

1. **Settings → System → Power & sleep**: set Sleep to **Never** (plugged in)
2. **Control Panel → Power Options → Change what closing the lid does**: set "When I close the lid" to **Do nothing** (plugged in)

> Windows 11 Modern Standby can still suspend on lid close even when sleep is disabled. Setting the lid action explicitly prevents this.

### Creating the scheduled tasks

Open PowerShell **as Administrator** and run:

```powershell
# Mon–Sat at 09:40 AM
schtasks /create /tn "TradingApp\DailyRun" /tr '"C:\Users\jbren\OneDrive\Documents\VS_Code\trading_app\run_daily.bat"' /sc WEEKLY /d MON,TUE,WED,THU,FRI,SAT /st 09:40 /rl HIGHEST /f

# Sunday at 01:00 AM
schtasks /create /tn "TradingApp\WeeklyRun" /tr '"C:\Users\jbren\OneDrive\Documents\VS_Code\trading_app\run_weekly.bat"' /sc WEEKLY /d SUN /st 01:00 /rl HIGHEST /f
```

Verify registration:
```powershell
schtasks /query /tn "TradingApp\DailyRun"  /fo LIST
schtasks /query /tn "TradingApp\WeeklyRun" /fo LIST
```

> **Note:** Tasks are created with **Interactive only** logon mode — they run when you are logged in. If the machine reboots and you have not logged back in by 09:40 AM, that day's run will be skipped. To change this, open Task Scheduler → TradingApp → task Properties → General → select **"Run whether user is logged on or not"**.

---

## Verify scripts

Run these to confirm each layer is working correctly before running the full pipeline.

```bash
python scripts/verify_connection.py    # IB Gateway / TWS paper account connection
python scripts/verify_pipeline.py      # data pipeline + indicators end-to-end
python scripts/verify_signals.py       # ML signal generation end-to-end
python scripts/verify_universe.py      # universe selection (requires Alpaca API keys)
python scripts/verify_risk.py          # risk & portfolio management layer
python scripts/test_ibkr_news.py       # fetch a few IBKR headlines for one symbol
```

---

## Operational tools

Ad-hoc scripts for managing the paper account outside the daily run. All require IB Gateway to be running.

### List / cancel open orders — `open_orders.py`

Default is read-only — lists every parked order on the paper account. Add `--cancel` to take action (useful for clearing stale GTC brackets from a previous `signal_runner --no-dry-run`).

```bash
# Listing
python scripts/open_orders.py                        # list all open orders (default)
python scripts/open_orders.py --symbol ABBV          # list only ABBV legs
python scripts/open_orders.py --symbol ABBV CL       # multiple symbols

# Cancelling (requires --cancel + one selector)
python scripts/open_orders.py --cancel --id 52 53 54 # cancel specific order IDs
python scripts/open_orders.py --cancel --symbol ABBV # cancel every open leg on ABBV
python scripts/open_orders.py --cancel --all         # cancel everything (prompts for confirmation)
python scripts/open_orders.py --cancel --all --yes   # same, no prompt
```

Cancelling the parent leg of a bracket auto-cancels the child TP/stop legs via OCA linkage, but passing all three IDs is safe — IBKR no-ops already-cancelled IDs.

### List / close positions — `open_positions.py`

Default is read-only — lists every held position. Add `--close` plus a selector to flatten with market sells. Different from `open_orders.py`: that manages *parked* orders, this manages *filled* positions.

```bash
# Listing
python scripts/open_positions.py                             # list all positions (default)
python scripts/open_positions.py --symbol AAPL               # list only AAPL
python scripts/open_positions.py --symbol AAPL MSFT          # multiple symbols

# Closing (requires --close + one selector)
python scripts/open_positions.py --close --symbol AAPL          # market-sell full AAPL
python scripts/open_positions.py --close --symbol AAPL --qty 1  # sell exactly 1 share
python scripts/open_positions.py --close --symbol AAPL MSFT     # close multiple positions in full
python scripts/open_positions.py --close --all                  # close every position (prompts)
python scripts/open_positions.py --close --all --yes            # same, no prompt
```

Only long positions are closed; shorts and zero-qty ghost positions are skipped with a warning. `--qty` requires exactly one `--symbol`.

---

## Configuration

Settings are edited in the dashboard (Settings page) or directly in `config/settings.yaml`. Secrets (API keys) are never written to the YAML file — set them as environment variables.

```bash
# Required for Alpaca news and universe selection
set ALPACA_API_KEY=your_key
set ALPACA_SECRET_KEY=your_secret
```

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `data.watchlist` | 10 large-caps | Symbols used when universe is disabled |
| `universe.enabled` | False | Use dynamic universe instead of static watchlist |
| `universe.stage3_max` | 50 | Max candidates after Stage 3 scoring |
| `trading.mode` | SIMULATION | SIMULATION or LIVE |
| `trading.paper_equity` | $100,000 | Assumed equity for dry-run sizing when no IBKR connection |
| `trading.cash_reserve_pct` | 0.20 | Fraction of equity kept in cash; positions sized against the rest |
| `trading.max_position_size_pct` | 0.05 | Hard cap on any single position (5% of equity) |
| `trading.paper_orders_enabled` | False | Set True to submit to IBKR paper account |
| `ml.signal_threshold` | 0.35 | Minimum ensemble score to generate a signal |
| `ml.signal_confirmation` | 2 | Models that must agree (of 3) |
| `ml.wf_n_splits` | 5 | Walk-forward folds per training run |
| `risk.kelly_fraction` | 0.25 | Quarter-Kelly multiplier for position sizing |
| `risk.atr_stop_multiplier` | 2.0 | Stop = entry ± ATR × this |
| `risk.atr_take_profit_multiplier` | 3.0 | Take-profit = entry ± ATR × this |
| `risk.circuit_breaker_daily_loss_pct` | 0.03 | 3% daily loss triggers trading halt |
| `risk.trailing_stop_enabled` | False | Opt-in: convert bracket TPs to trailing stops once a position is +N ATR in profit |
| `risk.trailing_stop_activation_atr` | 2.0 | Convert once `price ≥ entry + N × ATR` |
| `risk.trailing_stop_trail_atr` | 2.0 | Trailing distance below the highest reached price, in ATR units |

---

## ML models

### Signal pipeline

Each symbol passes through three models whose scores are combined into an ensemble:

| Model | Input | Output |
|-------|-------|--------|
| **LSTM** | 60-bar rolling window of OHLCV + 11 indicators | Score in [-1, 1] |
| **XGBoost** | 12 indicator + 13 fundamental features | Score in [-1, 1] |
| **FinBERT** | Recent news headlines (time-decay weighted) | Score in [-1, 1] |
| **Ensemble** | Weighted sum of above (default 40/35/25) | Score in [-1, 1] |

### Signal gate (three filters, all must pass)

1. `|ensemble_score| >= signal_threshold` (default 0.35)
2. Regime-adjusted threshold: HIGH_VOLATILITY raises by 1.5×, TRENDING lowers by 0.9×
3. At least `signal_confirmation` (default 2) of 3 models agree on direction

### Walk-forward training

Models are trained using time-series cross-validation to prevent lookahead bias. Default: 5 folds × (120 train + 21 test bars). After all folds, the ensemble is retrained on the full dataset for live inference.

```
run_pipeline.py     →  data in SQLite
train_models.py     →  models/cache/{symbol}/lstm.pt + xgb.ubj
signal_runner.py    →  loads checkpoints, generates signals, logs decisions
```

---

## Risk management

Every signal passes through a seven-check sequential guard before an order is considered:

1. **Circuit breaker** — halt active? (3% daily / 7% weekly loss triggers)
2. **Stop-price sanity** — stop on the loss side of entry? (BUY needs `stop < entry`, SELL needs `stop > entry`; catches bad ATR → zero-distance stops)
3. **Portfolio drawdown** — portfolio-wide loss exceeds limit?
4. **Position size** — proposed size exceeds `max_position_size_pct`?
5. **Sector exposure** — adding this position would exceed 30% in one sector?
6. **Correlation** — too many highly correlated positions already held?
7. **Duplicate** — already holding this symbol? (GOOG/GOOGL treated as same underlying)

Sizing additionally short-circuits to `REJECTED_TOO_SMALL` if Kelly output is below 1 share, so 0-share bracket orders never reach IBKR. Position sizing uses fractional Kelly criterion (quarter-Kelly by default) with **ATR-based stops** — the stop distance adapts to each stock's typical daily volatility instead of using a fixed percentage. When there is insufficient signal history (<10 trades), a fixed fallback sizes to risk 1% of investable equity per trade.

For SELL signals with `allow_short_selling=False` (the default), the order manager intercepts before sizing: if a long is held the position is flattened (`CLOSED_LONG`); otherwise the signal is ignored (`REJECTED_NO_POSITION`). Shorts are never opened.

Approved orders are submitted as **bracket orders** — three linked legs (entry + stop + take-profit) so risk and reward are both defined before the trade is on. All legs are GTC so they survive outside regular trading hours. When `trailing_stop_enabled=True`, each daily run evaluates every open long and may replace its bracket TP+stop with a **standalone trailing stop** once the position is sufficiently in profit — the stop then ratchets up with price and only triggers on reversal. See [docs/08-risk-management.md](docs/08-risk-management.md) for ATR fundamentals, bracket-order mechanics, and the trailing-stop conversion rules.

---

## News sources

News is fetched in priority order with automatic fallback:

| Source | Quality | Requires |
|--------|---------|---------|
| **IBKR** | Best — Dow Jones + Briefing.com, ~4 months history | IB Gateway or TWS running |
| **Alpaca** | Good — broad coverage, configurable lookback | `ALPACA_API_KEY` env var |
| **yfinance** | Fallback — ~10 most recent articles only | Nothing |

Run `run_pipeline.py` with IB Gateway open to get full IBKR news history. The Data Status page (Page 6) shows per-symbol news source and article counts.

---

## Tests

```bash
.venv\Scripts\pytest tests\ -v              # all tests (no network or TWS needed)
.venv\Scripts\pytest tests\test_risk.py -v  # risk module only
```

Tests use mocks for all external dependencies — no live IBKR connection, yfinance, or network calls required.

---

## Database

SQLite at `db/trading.db` (auto-created on first run). Key tables:

| Table | Contents |
|-------|----------|
| `ohlcv_bars` | OHLCV + ^VIX bars, all symbols and intervals |
| `indicator_snapshots` | RSI, MACD, Bollinger, EMA, ATR per bar |
| `fundamental_data` | P/E, revenue growth, margins etc. (24h cache) |
| `news_cache` | Headlines + FinBERT sentiment scores |
| `signal_log` | Every ensemble prediction and gate result |
| `walk_forward_results` | Per-fold Sharpe, drawdown, win rate |
| `universe_assets` | Dynamic universe candidates and their scores |
| `order_decisions` | Every DRY_RUN / APPROVED / REJECTED decision |
| `circuit_breaker_log` | All halt trigger and reset events |
| `signal_runner_log` | Daily run summaries |
