# LSTM Model

## What is an LSTM?

**LSTM** stands for Long Short-Term Memory. It's a type of **recurrent neural network (RNN)** designed to learn patterns in sequential data — data where the order of observations matters.

For financial time series, order matters enormously. A stock that has risen for 10 consecutive days is in a fundamentally different state than one that fell for 10 days then rose today, even if today's price is identical in both cases. LSTMs can capture this kind of temporal context.

---

## From neurons to sequences

### A single neuron (refresher)
A basic neural network neuron takes inputs, multiplies them by learned weights, adds a bias, and applies an activation function:

```
output = activation(w₁x₁ + w₂x₂ + ... + wₙxₙ + b)
```

### The recurrence problem
In a standard feedforward network, each input is processed independently. To process a sequence, you'd need to feed the entire sequence at once, with fixed length.

An RNN solves this by maintaining a **hidden state** — a memory vector that gets updated at each timestep:

```
h_t = tanh(W_h × h_{t-1} + W_x × x_t + b)
```

Where:
- `h_t` = hidden state at timestep t (the "memory")
- `h_{t-1}` = previous hidden state
- `x_t` = input at timestep t (today's features)
- `W_h`, `W_x` = learned weight matrices

### The vanishing gradient problem
Plain RNNs struggle to learn long-range dependencies. During training, gradients (the error signals used to update weights) flow backward through time. After many timesteps, these gradients shrink exponentially — the network effectively forgets events from more than ~10 timesteps ago.

### How LSTM solves it
LSTMs add a **cell state** — a separate memory track that runs alongside the hidden state. Four learned gates control what information flows in and out:

```
Forget gate:   f_t = σ(W_f × [h_{t-1}, x_t] + b_f)
Input gate:    i_t = σ(W_i × [h_{t-1}, x_t] + b_i)
Cell update:   C̃_t = tanh(W_C × [h_{t-1}, x_t] + b_C)
Output gate:   o_t = σ(W_o × [h_{t-1}, x_t] + b_o)

Cell state:    C_t = f_t × C_{t-1} + i_t × C̃_t
Hidden state:  h_t = o_t × tanh(C_t)
```

In plain English:
- **Forget gate** (f): "What should I erase from memory?"
- **Input gate** (i) + **cell update** (C̃): "What new information should I store?"
- **Output gate** (o): "What should I output based on my current memory?"

The cell state can carry information across hundreds of timesteps with gradients that don't vanish, because the forget gate's multiplicative path bypasses the tanh nonlinearity.

---

## The LSTM in this project

### Architecture

```python
LSTMModel(
    input_size  = 17,    # features per bar
    hidden_size = 128,   # neurons in each LSTM layer
    num_layers  = 2,     # stacked LSTM layers
    dropout     = 0.2,   # regularization between layers
    output_size = 1,     # single score in [-1, 1]
)
```

Two stacked LSTM layers: the first layer processes the raw input sequence and passes its hidden states to the second layer, which learns higher-level patterns. Dropout (20%) randomly zeros some connections during training to prevent overfitting.

### Input: 17 features × 60 bars

The model sees a **rolling window** of the last 60 trading bars (approximately 3 months). Each bar contains 17 features:

| Feature | Description |
|---------|-------------|
| Open | Opening price |
| High | Intraday high |
| Low | Intraday low |
| Close | Closing price |
| Volume | Shares traded |
| rsi_14 | Relative Strength Index (14-bar) |
| macd | MACD line (EMA-12 − EMA-26) |
| macd_signal | MACD signal line (EMA-9 of macd) |
| macd_hist | MACD histogram (macd − macd_signal) |
| bb_upper | Bollinger Band upper (SMA-20 + 2σ) |
| bb_middle | Bollinger Band middle (SMA-20) |
| bb_lower | Bollinger Band lower (SMA-20 − 2σ) |
| ema_9 | 9-bar exponential moving average |
| ema_21 | 21-bar exponential moving average |
| ema_50 | 50-bar exponential moving average |
| atr_14 | Average True Range (14-bar) |
| volume_sma_20 | 20-bar simple moving average of volume |

The canonical list lives in `models/lstm_model.py:_FEATURE_COLS` — that's the source of truth if these ever fall out of sync.

Each feature is **normalized using statistics from the training window only** — no data from the test set is used to compute mean or standard deviation. This prevents a subtle form of lookahead bias called **feature leakage**.

```python
# Computed from training data only
mean = train_df.mean()
std  = train_df.std()

# Applied to both train and test
train_normalized = (train_df - mean) / std
test_normalized  = (test_df  - mean) / std  # uses train stats
```

### Output: direction prediction

The LSTM outputs a single value through a `tanh` activation, producing a score in [-1, 1]:
- Near +1 → model strongly predicts upward movement
- Near -1 → model strongly predicts downward movement
- Near 0 → model is uncertain

The model is trained to predict the **sign of the 5-bar forward return**: will the price be higher or lower in 5 trading days? This is a binary classification problem framed as a regression (using tanh rather than softmax) to produce a continuous confidence score rather than just up/down.

### Training

```python
# Loss function: Binary Cross Entropy
criterion = nn.BCEWithLogitsLoss()

# Optimizer: Adam (adaptive learning rate)
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

# Training loop
for epoch in range(50):
    for batch in dataloader:
        pred = model(batch.x)
        loss = criterion(pred, batch.y)
        loss.backward()
        optimizer.step()
```

**Adam optimizer** adapts the learning rate for each parameter individually, generally converging faster than plain stochastic gradient descent for sequence models.

### Checkpoint format

Weights are saved to `models/cache/{symbol}/lstm.pt` using PyTorch's `state_dict` format. The checkpoint also stores the normalization parameters (mean and std as torch tensors) and the feature list so the model can be loaded without needing to refit normalizers.

```python
torch.save({
    "state_dict": model.state_dict(),
    "mean":       torch.tensor(mean.values),
    "std":        torch.tensor(std.values),
    "features":   feature_names,   # plain list, safe for weights_only=True
}, path)
```

Loading with `weights_only=True` (PyTorch ≥ 2.6 default) requires that all saved objects are safe Python types — torch tensors and lists qualify; pandas Series do not.

---

## score_series(): bar-by-bar inference

For the LSTM Analysis charts on dashboard Page 3, `score_series(df)` runs inference across an entire historical DataFrame, returning one score per bar:

```python
scores = lstm_model.score_series(df)
# Returns: pd.Series with same DatetimeIndex as df
# First 60 bars → NaN (no complete window yet)
# Bar 61 onward → score in [-1, 1]
```

This is computationally expensive compared to single-bar inference but enables the overlaid price + score chart that makes it easy to see where the model was bullish or bearish historically.

---

## What the LSTM learns (and doesn't)

**Learns well:**
- Short-to-medium momentum patterns (3–12 bars)
- Volume confirmation of price moves
- RSI divergence (price rising but RSI falling)
- Mean reversion in range-bound markets

**Struggles with:**
- Sudden regime changes (earnings surprises, macro shocks)
- Anything not visible in price/volume data (news, fundamentals)
- Very long-range dependencies (the forget gate eventually forgets)
- Low-volume symbols where price is noisy

This is why LSTM is one of three models in the ensemble — XGBoost contributes fundamental context and FinBERT contributes news awareness that the LSTM is blind to.
