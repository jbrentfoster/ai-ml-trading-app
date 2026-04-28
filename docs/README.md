# Documentation Index

Tutorials and reference guides for the AI Trading App. Each document is self-contained and written to explain both the *what* and the *why* behind each component.

---

## Reading order (recommended)

| # | Document | What you'll learn |
|---|----------|-------------------|
| 1 | [System Overview](01-system-overview.md) | End-to-end architecture, data flow, design decisions |
| 2 | [Data Pipeline](02-data-pipeline.md) | yfinance, technical indicators, SQLite storage |
| 3 | [Walk-Forward Validation](03-walk-forward.md) | Why standard cross-validation fails for time series |
| 4 | [LSTM Model](04-lstm.md) | Sequence learning, RNNs, how LSTM reads price history |
| 5 | [XGBoost Model](05-xgboost.md) | Gradient boosting, decision trees, feature importance |
| 6 | [FinBERT & Sentiment](06-finbert-sentiment.md) | Transformers, BERT, NLP applied to financial news |
| 7 | [Ensemble & Signal Gate](07-ensemble-signals.md) | Combining models, regime detection, signal filtering |
| 8 | [Risk Management](08-risk-management.md) | Kelly criterion, ATR stops, bracket orders, trailing stops, circuit breaker, portfolio guard |
| 9 | [Universe Selection](09-universe-selection.md) | 3-stage funnel for dynamic symbol selection |
| 10 | [Python Packages Reference](10-python-packages.md) | Key libraries used and why |

---

## Quick reference

- **Running the system:** see the project [README](../README.md)
- **Configuration:** `config/settings.yaml` — edit via the Settings page in the dashboard
- **Database schema:** described in [CLAUDE.md](../CLAUDE.md)
- **Tests:** `tests/` — run with `.venv\Scripts\pytest tests\ -v`
