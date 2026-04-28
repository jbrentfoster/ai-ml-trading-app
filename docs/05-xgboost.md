# XGBoost Model

## What is gradient boosting?

XGBoost implements **gradient boosted decision trees** — one of the most powerful and widely used algorithms in tabular machine learning. To understand it, we need to build up from decision trees.

---

## Decision trees

A decision tree splits data based on feature thresholds, recursively, until it reaches a prediction:

```
                    RSI < 30?
                   /          \
                Yes             No
                 |               |
         MACD > 0?          EMA20 > EMA50?
         /      \             /        \
       Yes       No         Yes          No
       BUY      HOLD        BUY         SELL
```

Single trees are fast and interpretable but tend to overfit — they memorize the training data rather than learning generalizable patterns.

### Random Forests
Random Forest builds many trees, each trained on a random subset of data and features, then averages their predictions. Averaging reduces overfitting (the errors of individual trees tend to cancel out). But Random Forest trains trees **independently** — each tree doesn't know what the others learned.

---

## Gradient boosting: learning from mistakes

Gradient boosting takes a different approach: trees are built **sequentially**, with each new tree focused on correcting the errors of all previous trees.

### The algorithm

1. Start with a simple prediction (e.g., the mean return)
2. Compute the **residuals** — the difference between predicted and actual
3. Train a new tree to predict the residuals
4. Add the new tree to the ensemble (with a small learning rate to avoid overshooting)
5. Repeat for N trees

```
Prediction_n = Prediction_{n-1} + learning_rate × Tree_n(residuals)
```

This is called "gradient" boosting because computing the residuals is equivalent to computing the gradient of the loss function — we're doing gradient descent in function space.

### XGBoost improvements

[XGBoost](https://xgboost.readthedocs.io/) (eXtreme Gradient Boosting) adds several improvements over basic gradient boosting:

- **Second-order gradients**: uses both gradient and Hessian (curvature) for faster, more accurate tree splits
- **Regularization**: L1 and L2 penalties prevent individual trees from being too complex
- **Parallel tree construction**: splits are evaluated in parallel across features
- **Missing value handling**: learns the best direction for missing features automatically
- **Subsampling**: each tree is trained on a random subset of rows and columns (like Random Forest), reducing overfitting

---

## The XGBoost model in this project

### Configuration

```python
XGBoostClassifier(
    n_estimators    = 300,   # number of trees
    max_depth       = 5,     # maximum tree depth
    learning_rate   = 0.05,  # shrinkage factor per tree
    subsample       = 0.8,   # 80% of rows per tree
    colsample_bytree= 0.8,   # 80% of features per tree
    objective       = "binary:logistic",
)
```

`n_estimators=300` with `learning_rate=0.05` means the model learns slowly but thoroughly — each tree corrects a small fraction of the previous error. This combination tends to generalize better than fewer trees with a higher learning rate.

### Features: 25 total

Unlike the LSTM (which sees raw sequences), XGBoost receives a **snapshot** of the current bar: 12 technical indicators and 13 fundamental metrics.

#### 12 Technical indicators
| Feature | Source |
|---------|--------|
| RSI-14 | `indicator_snapshots` |
| MACD | `indicator_snapshots` |
| MACD Signal | `indicator_snapshots` |
| Bollinger Upper | `indicator_snapshots` |
| Bollinger Lower | `indicator_snapshots` |
| EMA-20 | `indicator_snapshots` |
| EMA-50 | `indicator_snapshots` |
| ATR-14 | `indicator_snapshots` |
| Volume SMA-20 | `indicator_snapshots` |
| BB %B | `indicator_snapshots` |
| BB Width | `indicator_snapshots` |
| MACD Histogram | `indicator_snapshots` |

#### 13 Fundamental metrics
| Feature | Source |
|---------|--------|
| P/E Ratio | `fundamental_data` |
| Forward P/E | `fundamental_data` |
| Price-to-Book | `fundamental_data` |
| EV/EBITDA | `fundamental_data` |
| Revenue Growth | `fundamental_data` |
| Earnings Growth | `fundamental_data` |
| Profit Margin | `fundamental_data` |
| Return on Equity | `fundamental_data` |
| Debt-to-Equity | `fundamental_data` |
| Current Ratio | `fundamental_data` |
| Free Cash Flow | `fundamental_data` |
| Analyst Target Price | `fundamental_data` |
| % to Analyst Target | derived |

Fundamentals are cached in SQLite with a 24-hour TTL and fetched from yfinance via `FundamentalsClient`. They change slowly (quarterly earnings), so daily caching is sufficient.

### Target variable

Same as LSTM: predict the **sign of the 5-bar forward return**.

```python
df["target"] = (df["Close"].shift(-5) > df["Close"]).astype(int)
# 1 = price higher in 5 days, 0 = price lower
```

### Output: score in [-1, 1]

XGBoost is trained as a binary classifier and outputs a probability [0, 1]. This is mapped to the ensemble's expected [-1, 1] range:

```python
score = 2 * model.predict_proba(X)[:, 1] - 1
# proba=0.9 → score= 0.8  (strongly bullish)
# proba=0.5 → score= 0.0  (neutral)
# proba=0.1 → score=-0.8  (strongly bearish)
```

---

## Feature importance

XGBoost provides built-in feature importance scores — how often each feature was used in a tree split (gain importance). This is displayed on dashboard Page 3 as a horizontal bar chart.

Common patterns you might see:

- **RSI and ATR** tend to be highly important — they capture overbought/oversold conditions and volatility that directly affect near-term price movements
- **EMA crossovers** (EMA-20 vs EMA-50 spread) often rank high for trend-following signals
- **Fundamental metrics** rank lower in importance for short-term (5-day) predictions but become more important over longer horizons
- **Free cash flow and ROE** are more important for value/quality signals than for momentum signals

Feature importance is specific to each symbol and market regime. Run the walk-forward training on a symbol and check Page 3 to see which features drove its signals.

---

## Why XGBoost alongside an LSTM?

| Aspect | LSTM | XGBoost |
|--------|------|---------|
| Input type | Sequential (60 bars) | Snapshot (current bar only) |
| Temporal awareness | High (sees history) | None (no sequential memory) |
| Fundamental awareness | None | Yes (13 fundamental features) |
| Training speed | Slow (GPU helps) | Fast (CPU, seconds) |
| Interpretability | Black box | Feature importance scores |
| Sample efficiency | Needs many bars | Works well with less data |
| Regime sensitivity | Baked into sequence patterns | Relies on current features |

XGBoost's strength is in **cross-sectional discrimination** — ranking symbols relative to each other based on current valuation and quality. An undervalued stock with strong earnings growth and momentum looks different from an overvalued stock with decelerating growth, and XGBoost captures this from a single snapshot of features.

The LSTM's strength is in **temporal pattern recognition** — it can detect that a stock has been making higher lows for three months even if its current RSI looks neutral.

Together they cover dimensions that neither covers alone.

---

## Checkpoint format

XGBoost models are saved in the `.ubj` format (Universal Binary JSON — XGBoost's native binary format):

```python
model.save_model("models/cache/AAPL/xgb.ubj")
model.load_model("models/cache/AAPL/xgb.ubj")
```

This format is more compact and faster to load than the older JSON format, and is fully cross-platform.
