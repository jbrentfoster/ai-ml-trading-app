# Archived docs — predictive-alpha v1 (retired 2026-06)

These are the technical tutorials for the **retired predictive-alpha system** (the
LSTM + XGBoost + FinBERT ensemble, the LLM news analyst, walk-forward validation,
universe rotation, and the old risk layer). They document code that now lives under
[`../`](../) (`archive/models`, `archive/scripts`, …) and the full pre-pivot
codebase at git tag **`v1.0-predictive-alpha`**.

They are **archived, not deleted** — they are the learning history that produced the
pivot to risk-premia harvesting. See
[`../../docs/strategy/pivot_decision_2026-06.md`](../../docs/strategy/pivot_decision_2026-06.md)
for *why*, and [`../../docs/strategy/risk_premia_harvesting.md`](../../docs/strategy/risk_premia_harvesting.md)
for the current system.

| File | Documents |
|---|---|
| `01-system-overview.md` | the predictive-alpha architecture end-to-end |
| `02-data-pipeline.md` | yfinance → SQLite OHLCV/fundamentals/news pipeline (largely still current) |
| `03-walk-forward.md` | walk-forward validation harness |
| `04-lstm.md` / `05-xgboost.md` / `06-finbert-sentiment.md` | the three base models |
| `07-ensemble-signals.md` | ensemble signal combination |
| `08-risk-management.md` | Kelly sizing, ATR stops, bracket/trailing orders, circuit breaker, portfolio guard |
| `09-universe-selection.md` | dynamic stock-universe rotation |
| `10-python-packages.md` | dependency walkthrough (largely still current) |
| `11-llm-news-analyst.md` | the 8B-LLM news re-attribution analyst |
| `index-old.md` | the original `docs/` reading-order index |

> Note: a few of these (the data pipeline, package list) still describe **kept**
> infrastructure. They were moved here as a set because the series as a whole
> documents the retired system; for current behaviour, `CLAUDE.md` is authoritative.
