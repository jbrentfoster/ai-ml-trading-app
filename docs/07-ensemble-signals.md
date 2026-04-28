# Ensemble & Signal Gate

## Why combine models?

No single model is best in all market conditions:

- **LSTM** excels at momentum and mean-reversion patterns but is blind to news and fundamentals
- **XGBoost** captures fundamental quality and valuation signals but has no temporal memory
- **FinBERT** reacts to news that hasn't yet appeared in price data but can't evaluate price action

An ensemble combines their complementary strengths. When all three agree, the signal is much more reliable than any individual model. When they disagree, the ensemble moderates — and the signal gate can require agreement before acting.

---

## Ensemble scoring

The ensemble score is a weighted sum of the three model outputs:

```
ensemble_score = w_lstm × lstm_score
              + w_xgb  × xgb_score
              + w_fb   × finbert_score

clipped to [-1, 1]
```

Default weights: `w_lstm=0.40`, `w_xgb=0.35`, `w_fb=0.25`

### Example calculation

| Model | Score | Weight | Contribution |
|-------|-------|--------|-------------|
| LSTM | +0.72 | 0.40 | +0.288 |
| XGBoost | +0.58 | 0.35 | +0.203 |
| FinBERT | +0.30 | 0.25 | +0.075 |
| **Ensemble** | | | **+0.566** |

With `signal_threshold=0.35`, this score of +0.566 passes the threshold and would proceed to the signal gate.

### FinBERT suppression

When FinBERT is suppressed (e.g., for walk-forward folds predating available news), its score is forced to 0.0 and its weight is distributed equally to LSTM and XGBoost:

```python
if suppress_finbert:
    w_lstm  += finbert_weight / 2
    w_xgb   += finbert_weight / 2
    w_finbert = 0.0
    finbert_score = 0.0
```

---

## Dynamic weight rebalancing

After each walk-forward fold, the ensemble rebalances based on performance. The process:

### Step 1: LSTM vs XGBoost competition
Compare Sharpe ratios from the just-completed test window:

```python
if lstm_sharpe > xgb_sharpe:
    # nudge weight toward LSTM
    transfer = min(ensemble_nudge, w_xgb - weight_floor)
    w_lstm += transfer
    w_xgb  -= transfer
else:
    # nudge weight toward XGBoost
    transfer = min(ensemble_nudge, w_lstm - weight_floor)
    w_xgb  += transfer
    w_lstm -= transfer
```

`ensemble_nudge` (default 0.10) caps how much weight can transfer per fold — this prevents a single lucky fold from completely dominating the weights.

### Step 2: FinBERT coverage scaling
FinBERT's weight is always computed from the configured baseline, scaled by coverage:

```python
w_finbert = configured_base_weight × finbert_coverage
```

This resets from the baseline each fold rather than accumulating — FinBERT's weight never drifts away from its intended starting point.

### Step 3: Apply floor and normalize
Each weight must be at least `weight_floor` (default 0.10):

```python
w_lstm    = max(w_lstm,    weight_floor)
w_xgb     = max(w_xgb,     weight_floor)
w_finbert = max(w_finbert,  weight_floor)

total = w_lstm + w_xgb + w_finbert
w_lstm    /= total
w_xgb     /= total
w_finbert /= total
```

### Why this matters

Over time, the weights reveal which model has been working in the current market environment. If XGBoost has been consistently gaining weight, fundamental factors may be dominating. If LSTM has been winning, momentum or mean-reversion patterns are dominant.

The Walk-Forward page (Page 4) shows the weight evolution as a stacked area chart across folds — a useful diagnostic for understanding what the ensemble has learned.

---

## Regime detection

Before applying the signal gate, the system classifies the current market regime using two signals:

### VIX-based regime

```python
if vix > 25:
    regime = HIGH_VOLATILITY
```

The CBOE VIX (^VIX) measures implied volatility of S&P 500 options — essentially, how much uncertainty the options market is pricing in. Above 25 is elevated fear; above 30 is historically associated with market stress or crisis.

### ADX-based regime

```python
elif adx > 25:
    regime = TRENDING
else:
    regime = MEAN_REVERTING
```

ADX (Average Directional Index) measures **trend strength** regardless of direction. Above 25 indicates a clear trend (up or down); below 25 indicates a choppy, range-bound market.

### Regime effects on the signal gate

```python
match regime:
    case HIGH_VOLATILITY:
        effective_threshold = base_threshold × 1.5
    case TRENDING:
        effective_threshold = base_threshold × 0.9
    case MEAN_REVERTING:
        effective_threshold = base_threshold
```

**HIGH_VOLATILITY**: Raise the bar. In volatile markets, model predictions are less reliable (more noise, faster reversals). Requiring a stronger signal reduces false positives at the cost of missing some real opportunities.

**TRENDING**: Lower the bar slightly. In trending markets, momentum signals are more reliable — even moderate ensemble scores often indicate real directional moves.

**MEAN_REVERTING**: Keep the default. No adjustment; the models perform as calibrated.

---

## The signal gate

Every ensemble score passes through three sequential filters. All three must pass for a signal to become BUY or SELL. If any filter fails, the result is HOLD.

### Filter 1: Threshold

```python
if abs(ensemble_score) < effective_threshold:
    return SignalResult(signal="HOLD", reason="below threshold")
```

The threshold filters out low-conviction signals. With `signal_threshold=0.35` in a normal regime:
- Score of +0.20 → HOLD (not strong enough)
- Score of +0.40 → proceeds to filter 2
- Score of -0.60 → proceeds to filter 2 (as SELL candidate)

### Filter 2: Direction

The sign of the ensemble score determines direction:
- Positive score → BUY candidate
- Negative score → SELL candidate

This is implicit in filter 1 (we check `abs(score) >= threshold`) but the direction is preserved for filters 2 and 3.

### Filter 3: Model confirmation

```python
# Count how many models agree on direction
agreements = sum([
    lstm_score > 0,     # LSTM says buy
    xgb_score > 0,      # XGBoost says buy
    finbert_score > 0,  # FinBERT says buy
])

if agreements < signal_confirmation:  # default: 2
    return SignalResult(signal="HOLD", reason="models disagree")
```

With `signal_confirmation=2`, at least 2 of 3 models must point the same direction. This filters out signals where the ensemble score is pulled up by one strong model while the others are neutral or disagree.

**Example: one model dominates**
- LSTM: +0.85 (strongly bullish)
- XGBoost: -0.10 (slightly bearish)
- FinBERT: -0.05 (slightly bearish)
- Ensemble: +0.40×0.85 + 0.35×(-0.10) + 0.25×(-0.05) = +0.30

Even though the ensemble is positive, only 1 of 3 models agrees → HOLD.

**Example: consensus**
- LSTM: +0.65
- XGBoost: +0.45
- FinBERT: -0.10 (slightly contrary)
- Ensemble: +0.40×0.65 + 0.35×0.45 + 0.25×(-0.10) = +0.39

2 of 3 models agree → passes confirmation → BUY (assuming score >= threshold).

---

## SignalResult dataclass

The gate returns a `SignalResult` with full detail:

```python
@dataclass
class SignalResult:
    signal:          str    # "BUY", "SELL", "HOLD"
    ensemble_score:  float
    lstm_score:      float
    xgb_score:       float
    finbert_score:   float
    regime:          str
    gate_reason:     str    # which filter blocked (if HOLD)
    passed_gate:     bool
```

This is persisted to `signal_log` and displayed on Page 3 of the dashboard, making it possible to audit exactly why each signal was generated or blocked.

---

## Reading the signal log (Page 3)

The signal log table on Page 3 shows every signal generated, with scores and gate status. Useful things to check:

**High ensemble score but HOLD** → look at `gate_reason`. If it's "models disagree", one model had a very strong view while the others were flat or opposite. This is worth investigating — is one model consistently contrarian on this symbol?

**Consistent HOLD with "below threshold"** → the signal is generating predictions but none are strong enough. The symbol may be range-bound (weak trend) or the models may not have sufficient data (check Data Status page).

**All BUY, never SELL** → check the regime. In a strong bull market with TRENDING regime, the threshold is lower and momentum signals dominate. This is expected behavior, not a bug.
