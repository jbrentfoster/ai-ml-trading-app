# Walk-Forward Validation

## Why standard cross-validation fails for time series

In most machine learning problems, cross-validation works by randomly shuffling data into train/test splits. A model trained on examples 1–1000 can be tested on examples 500–600 mixed in from the middle. This works fine when examples are independent.

**Financial time series are not independent.** Tomorrow's price depends on today's. If you shuffle the data and train on bars from 2023 while testing on bars from 2022, you're using the future to predict the past — a form of **lookahead bias** that produces falsely optimistic results.

### A concrete example of lookahead bias

Imagine training an XGBoost model using 2024 data (which includes a major market crash recovery) to predict returns in 2023 (pre-crash). The model "knows" about the recovery because it was trained on post-crash data. Its 2023 predictions will be suspiciously good — but only because it cheated.

In live trading, you only know what happened before today. Any validation method must enforce the same constraint.

---

## Walk-forward validation

Walk-forward validation solves this by ensuring **training always precedes testing in time**.

```
Full dataset (chronological)
├──────────────────────────────────────────────────────────────┤

Fold 1:
  TRAIN [████████████████████]  TEST [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓]

Fold 2:
  TRAIN [██████████████████████████████]  TEST [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓]

Fold 3:
  TRAIN [████████████████████████████████████████████]  TEST [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓]

...and so on
```

Each fold:
1. Train the model on all data up to the split point
2. Test on the next N bars (which the model never saw during training)
3. Record performance metrics (Sharpe ratio, max drawdown, win rate)
4. Advance the split point forward and repeat

### Key parameters

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `wf_train_bars` | 120 | ~6 months of daily data per training window |
| `wf_test_bars` | 21 | ~1 month of daily data per test window |
| `wf_gap_bars` | 1 | Gap between train end and test start (prevents leakage) |
| `wf_n_splits` | 5 | Number of folds |

**Minimum bars required:** `train_bars + gap_bars + n_splits × test_bars = 120 + 1 + 5×21 = 226 bars`

With 365 days of daily history, most symbols have ~252 trading days — comfortably above the minimum.

---

## How it's implemented

The walk-forward framework lives in two layers:

### Layer 1: `data/walk_forward.py` (model-agnostic)

```python
from data.walk_forward import WalkForwardSplit

splitter = WalkForwardSplit(
    train_bars=120,
    test_bars=21,
    gap_bars=1,
    n_splits=5,
)

for train_df, test_df in splitter.split(full_df):
    model.train(train_df)
    predictions = model.predict(test_df)
    metrics = compute_metrics(predictions, test_df)
```

`WalkForwardSplit` is completely model-agnostic — it can be used with any strategy function. This makes it useful for testing non-ML strategies too (e.g., a simple moving average crossover).

### Layer 2: `models/walk_forward.py` (ML-specific)

`MLWalkForwardOrchestrator` wires the ensemble models, signal gate, cost model, and database persistence together:

```python
orch = MLWalkForwardOrchestrator(symbol="AAPL")
results = orch.run(df)    # trains and tests all folds
orch.save_models(cache_dir)  # saves final model for live inference
```

Inside each fold, predictions are made **bar by bar** — for each test bar, the orchestrator calls `ensemble.predict(history_df, as_of=bar_ts)`, passing only the historical data available on that date. This prevents FinBERT from using future news during testing.

---

## Bracket simulator

Walk-forward doesn't just evaluate signals in isolation — it runs each fold's test window through a **bracket simulator** in `_run_test_window` that mirrors the live execution path. The intent is that WF P&L reflects what would *actually* have happened end-to-end, not just the raw direction of the model's score.

### What the simulator models

- **Entry timing**: signals at bar `t` close enter at bar `t+1` open (matches live runner behaviour).
- **ATR-based brackets**: at entry, place `stop = entry − atr_stop_multiplier × ATR` and `tp = entry + atr_take_profit_multiplier × ATR` using ATR from the bar *before* entry (no lookahead).
- **Intra-bar worst-case rule**: when both stop and TP lie inside `[Low, High]` on the same bar, the **stop** fills (conservative).
- **Gap-through**: if `Open ≤ stop` (long), fill at `Open`. The gap *is* the slippage; no extra stop-slippage charge on top.
- **Stop slippage**: intra-bar stop fills charged `stop_slippage_multiplier × slippage_pct` (default 2.0× the baseline). TP fills are exact (limit orders fill at limit or better).
- **Trailing-stop conversion**: pre-activation, check `Close ≥ entry + activation_atr × ATR`. Once active, ratchet `peak_price = max(peak_price, High)` and `trail_stop = peak_price − trail_atr × ATR`. New trailing-stop level applies bar `t+1` onward (today's High can't tighten today's stop).
- **Long-only gate**: when `trading.allow_short_selling=False` (the default), SELL signals from flat are no-ops and SELL signals after a long close the position without opening a short. Matches the live `OrderManager` behaviour.
- **Fold-end flatten**: any position still open at the last bar of the test window is force-flattened. Bracket exits and fold-end flatten are independent — whichever fires first closes the position.
- **Costs**: each closed trade is charged `slippage_pct + commission_per_share / entry_px` (plus stop-slippage on stops) to compute `pnl_pct`.

### Persistence to `trade_log`

Each closed trade is written to the `trade_log` table with:
- `source='walk_forward'`, `run_id`, `fold_index`
- `entry_ts`, `entry_px`, `exit_ts`, `exit_px`
- `exit_reason ∈ {stop, tp, trailing, signal_flip, fold_end, manual_close}`
- `shares` (Kelly-sized — see below), `pnl` (net of costs, in dollars), `pnl_pct`, `costs_charged`

The `pnl` field is **already net of costs**; `costs_charged` is exposed separately for display reconstruction. Never compute `net_pnl = pnl - costs_charged` — that double-counts fees.

These rows are the data source for the Trade History dashboard page (Page 10) and for realised-Kelly sizing (below).

---

## Realised-Kelly sizing in walk-forward

Once enough closed trades accumulate in `trade_log` for a symbol (default ≥ 30), the position sizer switches from the signal-score proxy to **realised** Kelly inputs:

```
win_rate     = wins / n_trades
avg_win_pct  = mean(pnl_pct for winning trades)
avg_loss_pct = |mean(pnl_pct for losing trades)|
b            = avg_win_pct / avg_loss_pct
f*           = (win_rate × b − (1 − win_rate)) / b
```

The orchestrator computes `kelly_history` once at the start of each fold with `as_of=fold.test_start`, `source='walk_forward'`, `run_id=self._run_id` — naturally forward-only (no future folds), naturally excludes trades from prior runs with different ensemble weights. Below the trade-count threshold, sizing falls back to the signal-score proxy. Method label `kelly_realised` vs `kelly_proxy` appears in logs and in the order-decisions table.

Per-bar P&L (and therefore the fold's Sharpe / drawdown) is intentionally size-agnostic — scaling every bar's contribution by a fold-constant `position_pct` is a no-op for Sharpe, and `pnl_pct` remains the right Kelly input. Only `shares`, dollar `pnl`, and `costs_charged` in `trade_log` reflect the Kelly-sized position.

---

## Performance metrics

After each test window, `compute_metrics()` calculates:

### Sharpe Ratio
```
Sharpe = mean(daily_returns) / std(daily_returns) × sqrt(252)
```
The square root of 252 annualizes the ratio (there are ~252 trading days per year). A Sharpe above 1.0 is good; above 2.0 is excellent; below 0 means the strategy lost money.

### Maximum Drawdown
The largest peak-to-trough decline during the test period:
```
max_drawdown = max((peak - trough) / peak)
```
A drawdown of -0.10 means the portfolio fell 10% from its high point before recovering. The circuit breaker monitors this in real time.

### Win Rate
```
win_rate = profitable_signals / total_signals
```
Note: win rate alone is misleading. A strategy with 40% win rate can still be profitable if its average win is much larger than its average loss (high reward-to-risk ratio).

---

## Ensemble weight rebalancing

After each fold, the orchestrator rebalances the ensemble weights based on which model performed better:

```
Fold 1 result: LSTM Sharpe = 1.2, XGBoost Sharpe = 0.8
→ nudge weight toward LSTM by up to ensemble_nudge (0.10)
→ apply weight floor (0.10 minimum per model)
→ normalize so weights sum to 1.0
```

FinBERT is excluded from the Sharpe competition — it can't be evaluated like a price model. Instead, its weight scales with **coverage**: the fraction of test-window bars where at least one news article existed. If FinBERT only has news for 30% of bars, its weight is scaled to 30% of its configured baseline.

---

## Survivorship bias warning

When `config.universe.enabled = True`, the universe was selected using today's data. Historical walk-forward folds may include symbols that were only selected in hindsight (i.e., they survived and grew large enough to enter the universe). This makes historical performance look better than it would have been in practice.

For unbiased backtests, use the static watchlist (leave `config.universe.enabled=False`, or call `run_pipeline.py --use-watchlist`).

The orchestrator logs a warning when it detects universe selection is active:
```
WARNING: Walk-forward results may reflect survivorship bias
```

Each `walk_forward_results` row carries a `universe_policy` column (`dynamic` | `static`) so the dashboard's Walk-Forward page can flag biased runs visually — an amber banner appears above the summary cards whenever any displayed row has `universe_policy='dynamic'`, and the detailed results table colour-codes the column.

---

## Quick mode vs full mode

| Setting | Full | Quick |
|---------|------|-------|
| LSTM epochs | 50 | 5 |
| XGBoost estimators | 300 | 50 |
| Folds | 5 | 2 |
| Train bars | 120 | 60 |
| Test bars | 21 | 10 |
| Min bars required | 226 | 81 |

Quick mode is useful for rapid iteration and debugging but trades significant model quality for speed. The LSTM in particular needs many epochs to converge — 5 epochs produces a partially trained model. **Do not use quick mode for production signals.**

---

## Interpreting Walk-Forward results (Page 4)

The dashboard's Walk-Forward page shows per-fold results. What to look for:

**Consistent Sharpe across folds** — if Sharpe varies wildly (1.5 in fold 1, -0.3 in fold 3), the model is unstable. Consistent but moderate performance is better than erratic peaks.

**Weight evolution** — the stacked area chart shows how ensemble weights shifted over folds. Rapid shifts suggest one model dominated a particular market regime. Gradual, stable weight distribution suggests the ensemble is well-calibrated.

**Sentiment note** — each fold records `finbert_coverage` (fraction of bars with news). If FinBERT coverage is low, its weight was automatically reduced. This is expected for folds covering periods before news data was available.
