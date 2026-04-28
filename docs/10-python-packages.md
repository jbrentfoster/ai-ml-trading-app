# Python Packages Reference

A guide to the key libraries used in this project — what they do, why they were chosen, and how they're used here.

---

## Data & storage

### yfinance
**What it is:** Unofficial Python wrapper around Yahoo Finance's market data API.
**Why it's used:** Free, no API key required, covers equities worldwide with decades of adjusted OHLCV history.
**How it's used:** `DataFetcher.fetch_symbol()` — incremental daily/hourly bar fetching with SQLite upsert.
**Docs:** https://github.com/ranaroussi/yfinance

```python
import yfinance as yf
df = yf.Ticker("AAPL").history(period="1y", interval="1d", auto_adjust=True)
info = yf.Ticker("AAPL").fast_info  # market cap, last price, etc.
```

**Limitations:** Rate-limited; occasional data gaps; unofficial API (no SLA). The pipeline is designed to be idempotent so re-runs are safe when yfinance has a hiccup.

---

### SQLAlchemy
**What it is:** Python SQL toolkit and ORM (Object-Relational Mapper).
**Why it's used:** Provides a clean Python interface to SQLite without raw SQL everywhere. The ORM layer defines table schemas as Python classes; the query helpers in `data/database.py` handle all reads and writes.
**How it's used:** Table definitions, upsert helpers, migration functions in `data/database.py`.
**Docs:** https://docs.sqlalchemy.org/

```python
from sqlalchemy import create_engine, Column, Float, String
engine = create_engine("sqlite:///db/trading.db")

# Upsert pattern (insert or replace)
with engine.begin() as conn:
    conn.execute(
        insert(OHLCVBar).prefix_with("OR REPLACE"),
        rows
    )
```

**Note:** All timestamps stored as UTC-naive datetimes to avoid SQLAlchemy timezone complexities. The `_migrate()` function handles schema evolution with idempotent `ALTER TABLE` statements.

---

### pandas
**What it is:** The standard Python library for tabular data manipulation.
**Why it's used:** Nearly universal in data science. DataFrames are the lingua franca between the pipeline, models, and dashboard.
**How it's used:** Everywhere — OHLCV bar processing, indicator computation, feature engineering, signal log queries.
**Docs:** https://pandas.pydata.org/docs/

```python
import pandas as pd
df = pd.read_sql("SELECT * FROM ohlcv_bars WHERE symbol=?", engine, params=["AAPL"])
df["returns"] = df["Close"].pct_change()
df["rolling_vol"] = df["returns"].rolling(20).std()
```

---

### pandas-ta
**What it is:** Technical analysis library built on top of pandas.
**Why it's used:** Implements 130+ indicators in a pandas-native API. Much faster than implementing RSI, MACD, etc. from scratch, and results are validated against established TA libraries.
**How it's used:** `IndicatorEngine` in `data/indicators.py` — RSI, MACD, Bollinger Bands, EMA, ATR, Volume SMA.
**Docs:** https://github.com/twopirllc/pandas-ta

```python
import pandas_ta as ta
df.ta.rsi(length=14, append=True)   # adds RSI_14 column
df.ta.macd(fast=12, slow=26, signal=9, append=True)
df.ta.bbands(length=20, std=2, append=True)
```

---

## Machine learning

### PyTorch
**What it is:** Deep learning framework from Meta. The dominant framework for research and increasingly for production.
**Why it's used:** Flexible dynamic computation graphs make it easier to debug than TensorFlow. `ib_insync` requirement on Python 3.10+ made compatibility important — PyTorch 2.x handles this well.
**How it's used:** `LSTMModel` in `models/lstm_model.py` — model architecture, training loop, checkpoint save/load.
**Docs:** https://pytorch.org/docs/

```python
import torch
import torch.nn as nn

class LSTMModel(nn.Module):
    def __init__(self, input_size, hidden_size, num_layers):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                            batch_first=True, dropout=0.2)
        self.fc = nn.Linear(hidden_size, 1)

    def forward(self, x):
        out, _ = self.lstm(x)
        return torch.tanh(self.fc(out[:, -1, :]))
```

**PyTorch 2.6+ note:** `torch.load()` defaults to `weights_only=True`, which requires all saved objects to be safe Python types (tensors, lists, dicts). The LSTM checkpoint stores normalization parameters as tensors (not pandas Series) for this reason.

---

### XGBoost
**What it is:** Optimized gradient boosting library. Won dozens of Kaggle competitions and is a workhorse for tabular ML in industry.
**Why it's used:** Fast training, handles missing values natively, built-in feature importance, no need for feature scaling (trees are invariant to monotonic transformations).
**How it's used:** `XGBoostModel` in `models/xgboost_model.py` — binary classification on 25 features, saved as `.ubj` (Universal Binary JSON).
**Docs:** https://xgboost.readthedocs.io/

```python
import xgboost as xgb
model = xgb.XGBClassifier(
    n_estimators=300, max_depth=5, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    objective="binary:logistic", eval_metric="logloss",
)
model.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
model.save_model("xgb.ubj")
```

---

### Transformers (Hugging Face)
**What it is:** Library providing thousands of pre-trained NLP models including BERT, GPT, and FinBERT.
**Why it's used:** Provides a simple `pipeline()` interface to FinBERT without having to implement BERT architecture from scratch. The model downloads automatically on first use (~400 MB).
**How it's used:** `FinBERTModel` in `models/finbert_model.py` — headline sentiment classification.
**Docs:** https://huggingface.co/docs/transformers/

```python
from transformers import pipeline
pipe = pipeline("text-classification", model="ProsusAI/finbert")
result = pipe("Company reports record quarterly earnings")
# → [{"label": "positive", "score": 0.97}]
```

**Model cache:** HuggingFace caches downloaded models in `~/.cache/huggingface/`. The first run downloads FinBERT; subsequent runs load from cache.

---

### scikit-learn
**What it is:** The standard Python machine learning library — classification, regression, clustering, preprocessing, model evaluation.
**Why it's used:** `StandardScaler`, `train_test_split`, and Pearson correlation utilities used throughout.
**How it's used:** Feature scaling (where needed), walk-forward metrics helpers, correlation computation in `PortfolioGuard`.
**Docs:** https://scikit-learn.org/stable/

```python
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score

scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)  # fit on train only
X_test_scaled  = scaler.transform(X_test)        # apply to test
```

---

## Dashboard

### Streamlit
**What it is:** Python library that turns scripts into interactive web apps with minimal boilerplate.
**Why it's used:** The entire 8-page dashboard is built with Streamlit — no HTML/CSS/JavaScript required. Each page is a Python script that runs top-to-bottom on every user interaction.
**How it's used:** All dashboard pages in `dashboard/`.
**Docs:** https://docs.streamlit.io/

```python
import streamlit as st

st.set_page_config(page_title="Market Data", layout="wide")
st.title("Market Data & Indicators")

symbol = st.sidebar.selectbox("Symbol", ["AAPL", "MSFT", "GOOGL"])
df = query_ohlcv(symbol)
st.plotly_chart(make_candlestick(df))
```

**Caching:** `@st.cache_data(ttl=300)` decorates all database query functions — results are cached for 5 minutes so the dashboard doesn't hammer SQLite on every widget interaction.

**Multi-page apps:** Each file in `dashboard/pages/` automatically becomes a sidebar navigation item. The file naming convention (`2_Fundamentals_News.py`) controls the order and label.

---

### Plotly
**What it is:** Interactive charting library supporting candlesticks, line charts, bar charts, heatmaps, area charts, and more.
**Why it's used:** Rich interactivity out of the box (zoom, hover tooltips, pan) that works natively with Streamlit via `st.plotly_chart()`. All charts follow a consistent dark theme.
**How it's used:** Every chart in every dashboard page.
**Docs:** https://plotly.com/python/

```python
import plotly.graph_objects as go

fig = go.Figure(data=[go.Candlestick(
    x=df.index,
    open=df["Open"], high=df["High"],
    low=df["Low"],   close=df["Close"],
)])
fig.update_layout(template="plotly_dark", margin=dict(l=0, r=0, t=40, b=0))
st.plotly_chart(fig, use_container_width=True)
```

**Chart conventions:** `template="plotly_dark"`, teal `#26a69a` for bullish/positive, red `#ef5350` for bearish/negative — consistent across all pages.

---

## IBKR connectivity

### ib_insync
**What it is:** Python wrapper around Interactive Brokers' Trader Workstation (TWS) API. Provides an async-friendly interface on top of IB's low-level socket protocol.
**Why it's used:** The official IB Python API (`ibapi`) is synchronous and requires threading to use correctly. `ib_insync` wraps it in asyncio, making it far easier to use in an async application.
**How it's used:** `IBKRConnection` in `execution/ibkr_connection.py` — connect, account summary, positions, orders, market data.
**Docs:** https://ib-insync.readthedocs.io/

```python
from ib_insync import IB, Stock, MarketOrder

ib = IB()
await ib.connectAsync(host="127.0.0.1", port=4002, clientId=1)

contract = Stock("AAPL", "SMART", "USD")
order = MarketOrder("BUY", 10)
trade = ib.placeOrder(contract, order)
```

**asyncio gotcha:** `ib_insync` imports `eventkit` which calls `asyncio.get_event_loop()` at import time. On Python 3.10+ in non-main threads (Streamlit's ScriptRunner), this raises a RuntimeError. The fix is to create a new event loop before importing — see `NewsClient._fetch_from_ibkr_standalone()` and `dashboard/pages/account.py` for the pattern.

---

## Scheduling & utilities

### schedule
**What it is:** Simple Python job scheduling library — "run this function every Sunday at 02:00".
**Why it's used:** Lightweight alternative to cron for Python processes. `universe_scheduler.py` uses it for its internal forever-loop mode.
**How it's used:** `universe_scheduler.py` — weekly and daily schedule for universe refresh.
**Docs:** https://schedule.readthedocs.io/

```python
import schedule, time

schedule.every().sunday.at("02:00").do(full_refresh)
schedule.every().monday.at("06:00").do(rescore)

while True:
    schedule.run_pending()
    time.sleep(60)
```

---

### alpaca-trade-api / alpaca-py
**What it is:** Official Python SDK for the Alpaca Markets API (commission-free stock trading + market data).
**Why it's used:** Secondary news source (after IBKR, before yfinance fallback) and universe Stage 1 asset listing.
**How it's used:** `NewsClient` (news fallback), `UniverseSelector._stage1_fetch()` (asset listing).
**Docs:** https://docs.alpaca.markets/

```python
from alpaca.trading.client import TradingClient
client = TradingClient(api_key, secret_key)
assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))
```

**Keys required:** `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` as environment variables. The free tier covers news and asset listing; no funded Alpaca account is needed.

---

## Testing

### pytest
**What it is:** The standard Python testing framework. More powerful and readable than the built-in `unittest`.
**Why it's used:** Simple fixture system, parametrize support, clear failure output, and excellent plugin ecosystem.
**How it's used:** All tests in `tests/`. Run with `.venv\Scripts\pytest tests\ -v`.
**Docs:** https://docs.pytest.org/

```python
import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    init_db(engine)
    return engine

def test_upsert_bars(mock_db):
    upsert_bars("AAPL", "1d", sample_df, engine=mock_db)
    result = get_bars("AAPL", "1d", engine=mock_db)
    assert len(result) == len(sample_df)
```

All tests mock external dependencies (yfinance, ib_insync, Alpaca API) so no network access or live IBKR connection is required. The `tests/` directory has one test file per major module:

| File | Coverage |
|------|---------|
| `test_data_pipeline.py` | DataFetcher, IndicatorEngine |
| `test_ibkr_connection.py` | IBKRConnection |
| `test_walk_forward.py` | WalkForwardSplit, compute_metrics, orchestrator |
| `test_models.py` | LSTM, XGBoost, FinBERT, RegimeDetector, SignalGate |
| `test_universe.py` | UniverseSelector, stage 1/2/3 |
| `test_risk.py` | PositionSizer, PortfolioGuard, CircuitBreaker, OrderManager |
