# Archive — predictive-alpha v1 (retired 2026-06-27)

This folder will hold the retired **predictive-alpha** modules after the physical
restructure. The project pivoted to risk-premia harvesting — see
[`../docs/strategy/pivot_decision_2026-06.md`](../docs/strategy/pivot_decision_2026-06.md)
and [`../docs/strategy/risk_premia_harvesting.md`](../docs/strategy/risk_premia_harvesting.md).

**Nothing is lost:** the complete pre-restructure state is preserved at git tag
**`v1.0-predictive-alpha`** (`git checkout v1.0-predictive-alpha` to see the full v1
codebase). The retired code is *archived, not deleted* — it is the learning history
that produced the pivot decision.

---

## What is retired (to be moved here)

| Group | Files |
|---|---|
| ML ensemble | `models/lstm_model.py`, `xgboost_model.py`, `finbert_model.py`, `ensemble.py`, `signal_gate.py`, `regime_detector.py`, `walk_forward.py`, `base_model.py`, `trade_patterns.py` |
| LLM news cluster | `models/llm_analyst.py`, `data/news_dedup.py`, `scripts/ingest_news_bodies.py`, `score_news_llm.py`, `validate_news_scores.sql`, `analyze_news_scores.py`, `spike_body_availability.py`, `bench_llm_extraction.py` |
| Signal/training/universe scripts | `scripts/train_models.py`, `signal_runner.py`, `universe_scheduler.py`, `intraday_check.py`, `benchmark_train_bars.py`, `data/universe.py` (stock rotation) |
| Retired tests | `tests/test_models.py`, `test_walk_forward.py`, `test_llm_analyst.py`, `test_news_dedup.py`, `test_trade_patterns.py`, `test_signal_runner.py`, `test_universe.py` |
| Retired dashboard pages | Pages 3 (Model Signals), 4 (Walk-Forward), 7 (Universe), 11 (LLM News); Page 10 forensics section |

## What STAYS (the salvaged infrastructure — do NOT archive)

`execution/` (ibkr_connection, reconciliation), `data/flex_client.py`, `data/database.py`,
`data/fetcher.py`, `data/indicators.py`, `data/walk_forward.py` (the **model-agnostic**
framework — reused by the new portfolio backtester), `risk/circuit_breaker.py`, the
dashboard shell + market-data/account/trade-history pages, `config/`, `core/`.

## Coupling to decouple BEFORE the physical move (mapped 2026-06-27)

A plain `git mv` breaks these — handle first, then move, then run the test suite:

1. **`risk/order_manager.py:36`** `from models.signal_gate import SignalResult`. order_manager is mostly retired; salvage only the bracket/market-order *submission* helpers the new rebalancer needs into a slim new module (e.g. `execution/orders.py`), dropping the `SignalResult` dependency.
2. **`dashboard/pages/10_Trade_History.py:46`** `from models.trade_patterns import …`. Keep the trade-history view; remove the Forensics section (it depends on `trade_patterns`).
3. **`data/ui_queries.py`** `query_llm_news_analysis` → `data/news_dedup` → `models/llm_analyst`. Remove the LLM query + its callers (Page 11) so `news_dedup`/`llm_analyst` can move.

## Safe restructure sequence (each step: move a batch, then `pytest` the survivors)

1. Decouple (1)–(3) above; commit.
2. Move the **LLM cluster** (self-contained after step 3) → `archive/`; commit + test.
3. Move the **ML ensemble** `models/` (after step 1) + retired tests → `archive/`; commit + test.
4. Move retired **scripts** + `data/universe.py` → `archive/`; commit + test.
5. Trim retired **dashboard pages**; commit + smoke-test the dashboard.
6. **Rewrite CLAUDE.md** around the new architecture (the v1 body is retained until then; the pivot banner at the top already redirects readers).
