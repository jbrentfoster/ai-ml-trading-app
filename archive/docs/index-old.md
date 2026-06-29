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
| 11 | [LLM News Analyst](11-llm-news-analyst.md) | Body-reading shadow workflow — composite scoring, attribution, event dedup (Page 11, not consumed by signal_runner) |

---

## Process

| Document | What it covers |
|----------|----------------|
| [Review system](review-system.md) | How daily/weekly logs and individual trades flow through the review skills (`/daily-run-review`, `/weekly-run-review`, `/trade-case-study`) into `followups.md`, CLAUDE.md, CHANGELOG.md, and `case_studies/` |

---

## Case studies

Per-trade post-mortems reconstructing one position's full decision context. Produced by the `/trade-case-study` skill; see [`case_studies/`](case_studies/).

| Trade | What it documents |
|-------|-------------------|
| [SNOW (2026-04)](case_studies/snow_2026-04.md) | First trailing-stop conversion in system history; Phase-B-confirmed exit + post-exit continuation |
| [AXTI (2026-04)](case_studies/axti_2026-04.md) | Matched failure case — TP fired overnight before the once-per-day trail could engage |
| [ASTS (2026-04)](case_studies/asts_2026-04.md) | Third trail conversion — partial-bar-refresh catch on day 14 |
| [MRVL (2026-05)](case_studies/mrvl_2026-05.md) | First case study on a **broker-reconciled** (Phase B) trade; gap-through TP exit-reason bug |
| [CSCO (2026-05)](case_studies/csco_2026-05.md) | The dog that didn't bark — a name the system never traded, and why |
| [Losers (2026-04→05)](case_studies/losers_2026-05.md) | WFC / TMUS / TEL — stop-out, slow bleed, and LSTM/MACD disagreement |
| [AZN (2026-06)](case_studies/azn_2026-06.md) | LSTM-saturated-bearish held long with no exit but the ATR stop (FinBERT propped the ensemble) |
| [SNDK (2026-05)](case_studies/sndk_2026-05.md) | Trailing-stop winner — first trail case study with a Phase-B-confirmed exit |

## Findings

Distributional patterns across *many* trades — confirmed aggregate observations that need diagnosis before any fix. A finding documents what we know and what we'd need to know next, not the answer. Scope rule + lifecycle in [`findings/README.md`](findings/README.md).

| Finding | Status |
|---------|--------|
| [Stop-out bleed](findings/stop_bleed.md) | Strategy-decided stops underperform SPY by ~7% (observed/hypothesized 2026-05-19) |
| [TP concentration](findings/tp_concentration.md) | 18% of trades carry the entire alpha (observed/hypothesized 2026-05-19) |
| [WF Sharpe vs live outcome](findings/wf_vs_live_correlation.md) | Weak positive correlation, not a veto (observed/hypothesized 2026-06-01) |
| [News attribution misallocation](findings/news_attribution_misallocation.md) | Feed-tagged news is about the feed symbol only ~36% of the time (observed/hypothesized 2026-06-04) |

---

## Quick reference

- **Running the system:** see the project [README](../README.md)
- **Configuration:** `config/settings.yaml` — edit via the Settings page in the dashboard
- **Database schema:** described in [CLAUDE.md](../CLAUDE.md)
- **Tests:** `tests/` — run with `.venv\Scripts\pytest tests\ -v`
