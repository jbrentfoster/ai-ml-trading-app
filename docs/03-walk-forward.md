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

For unbiased backtests, use the static watchlist:
```bash
python train_models.py --use-watchlist
```

The orchestrator logs a warning when it detects universe selection is active:
```
WARNING: Walk-forward results may reflect survivorship bias
```

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
