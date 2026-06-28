# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A **personal risk-premia harvesting portfolio tool** that runs on Interactive Brokers (IBKR) via IB Gateway. It holds a diversified, value+quality-tilted ETF core plus a small concentrated Buffett-style stock satellite, and rebalances it on a slow cadence. Built as a learning / science project (and a possible legacy tool) on a small, drought-tolerant capital sleeve — **not** the operator's primary wealth, which lives in an index 401k + a managed account.

> ### History — this project pivoted (2026-06)
> It began as a **predictive-alpha** system (an LSTM/XGBoost/FinBERT ML ensemble + an LLM news analyst trading a rotating stock universe on daily signals). Four predictive-alpha directions were each tested with cheap probes and **retired on evidence** — durable alpha from commodity public data on a laptop kept not being there. The whole predictive layer is **archived, not deleted**.
>
> - **Strategy + plan:** [`docs/strategy/risk_premia_harvesting.md`](docs/strategy/risk_premia_harvesting.md)
> - **Why we pivoted:** [`docs/strategy/pivot_decision_2026-06.md`](docs/strategy/pivot_decision_2026-06.md)
> - **Evidence:** [`docs/findings/volatility_cohort_edge.md`](docs/findings/volatility_cohort_edge.md) + the `scripts/analyze_*.py` research scripts.
> - **Full pre-pivot codebase:** git tag **`v1.0-predictive-alpha`**; retired modules live under [`archive/`](archive/README.md).

Python: synchronous throughout except IBKR (async/await via `ib_insync`). Data via yfinance → SQLite. Dashboard is Streamlit + Plotly.

## Strategy (summary)

- **80% core:** a fixed, diversified ETF allocation tilted to value + quality. Pinned starting weights (`docs/strategy/risk_premia_harvesting.md` §6): VLUE 22 / QUAL 22 / EFV 8 / IEF 14 / GLD 8 / PDBC 6 (= 80%).
- **20% satellite:** 4–6 concentrated **large-cap** Buffett-style names from `scripts/buffett_screen.py` (quality + value + safety ranking) plus operator judgment. Capped at 20%; for upside optionality + learning, **not** modeled as reliable alpha.
- **No prediction, no per-position stops.** Returns come from harvesting the value+quality premium + patience (enduring multi-year value droughts that force institutions out) + low cost. Risk is managed by diversification, the value anchor (crash protection), the circuit breaker, and (deferred to v2) a light trend/vol overlay.
- **Success = risk-adjusted** (Sharpe, max drawdown, Calmar) vs a 60/40 benchmark, **not** beating SPY total return (it will lag SPY in growth bulls — that is the cost of diversification/discipline, by design).
- **The toll booth:** value can underperform for a decade (2007–2020). The premium exists *because* most can't endure that. The small/patient capital is the edge — see the strategy doc, including the honest full-cycle ETF reality check (≈ match SPY with crash-protection, not beat it).

## Setup

```bash
pip install -r requirements.txt
```

Run all commands from the project root (so `config`, `core`, `data`, `execution`, `portfolio`, etc. resolve as packages). Activate the `.venv` at `trading_app/.venv/` or prefix with `.venv/Scripts/python` on Windows.

IB Gateway: `Configure → Settings → API → Settings` — enable ActiveX and Socket Clients, socket port 4002 (paper) / 4001 (live), uncheck "Read-Only API".

## Commands

```bash
# Seed OHLCV + fundamentals (+ news headlines, no sentiment scoring) for the watchlist
python scripts/run_pipeline.py
python scripts/run_pipeline.py --skip-news        # OHLCV + fundamentals only

# End-of-day bar refresh (overwrite mid-day partial bars with post-close values)
python scripts/refresh_recent_bars.py

# Buffett-style screen → ranked large-cap shortlist for the 20% satellite
python scripts/buffett_screen.py                  # writes db/buffett_screen_latest.csv

# Rebalance (PLANNED — see "Allocation engine & rebalancer" below)
python scripts/rebalance.py                       # dry-run: show drift + proposed plan
python scripts/rebalance.py --no-dry-run          # submit (needs rebalance_orders_enabled)

# Reconcile live IBKR fills -> fill_log (+ historical trade_log)
python scripts/reconcile_flex.py                  # durable T+1 Flex backstop (no Gateway)
python scripts/reconcile_fills.py                 # in-session reqExecutions poll

# Ops CLIs
python scripts/open_positions.py                  # list/close held IBKR positions
python scripts/open_orders.py                     # list/cancel open IBKR orders

# Dashboard (multi-page Streamlit)
streamlit run dashboard/1_Market_Data.py

# Tests (no live Gateway or network needed; ib_insync/yfinance/DB are mocked)
.venv/Scripts/pytest tests/ -v
```

## File Structure (current)

```
trading_app/
├── CLAUDE.md
├── config/        settings.py (AppConfig + YAML), settings.yaml
├── core/          logger.py
├── execution/     ibkr_connection.py (async IBKR ctx mgr), reconciliation.py (fill → fill_log/trade_log)
├── data/          database.py (ORM + _migrate), fetcher.py, indicators.py, fundamentals.py,
│                  news_client.py, flex_client.py (Flex Web Service), sectors.py (sector
│                  classification — extracted from the retired portfolio_guard), ui_queries.py
├── risk/          circuit_breaker.py (KEPT).  order_manager / position_sizer / portfolio_guard /
│                  trailing_stop remain pending a final tidy-up (retired, self-contained, unused).
├── portfolio/     NEW — to build: allocation.py (pure engine), rebalancer.py (execution),
│                  holdings.py (current state).  See "Allocation engine & rebalancer".
├── dashboard/     1_Market_Data.py + pages/ {2 Fundamentals&News, 5 Settings, 6 Data Status,
│                  8 Risk&Portfolio, 9 Account, 10 Trade History}
├── scripts/       buffett_screen.py, run_pipeline.py, refresh_recent_bars.py, reconcile_fills.py,
│                  reconcile_flex.py, backfill_flex_trades.py, backfill_benchmark_returns.py,
│                  backfill_sectors.py, open_orders.py, open_positions.py, verify_connection.py,
│                  verify_pipeline.py, test_ibkr_news.py, analyze_*.py (pivot-evidence research).
│                  PLANNED: rebalance.py, set_targets.py.
├── tests/         mocked unit tests (data pipeline, ibkr connection, reconciliation, flex,
│                  circuit breaker/sector via test_risk, ui_queries, trade_log, sectors, …)
├── archive/       retired predictive-alpha code (models/, LLM cluster, universe, intraday,
│                  retired scripts/tests/dashboard pages).  See archive/README.md.
├── docs/          strategy/ (risk_premia_harvesting.md, pivot_decision_2026-06.md),
│                  findings/, case_studies/, reviews/, enhancements.md, tutorials
├── db/            trading.db (SQLite, gitignored), buffett_screen_latest.csv (gitignored)
└── batch_files/   run_daily/weekly/eod/intraday.bat — STALE (orchestrate archived scripts);
                   to be replaced by new rebalance automation.
```

## Data Flow

```
yfinance → DataFetcher.fetch_symbol() → upsert_bars()/upsert_indicators() → SQLite
FundamentalsClient.get() → fundamental_data (24h cache; feeds buffett_screen + sectors)

target_allocation (SQLite) ─┐
IBKR positions/NLV/cash     ─┤→ portfolio/allocation.compute_plan() → RebalancePlan
prices (IBKR/yfinance)      ─┘                                          │
                             portfolio/rebalancer (dry-run gated) → IBKR orders
                             → execution/reconciliation → fill_log → holdings/transactions
                             → dashboard (reads SQLite only, via data/ui_queries.py)
```

## Allocation engine & rebalancer (the new core)

Design confirmed 2026-06-28; build pending. Mirrors the codebase's pure-logic-module + execution-wrapper + CLI + dry-run two-gate patterns.

- **`target_allocation` table** (SQLite, via `_migrate`): one source of truth for desired holdings — `ticker, sleeve('core'|'satellite'), target_weight, label, active, updated_at`. Core rows set once (the pinned table); satellite rows rewritten after each `buffett_screen` + judgment pass. Edited via `scripts/set_targets.py`.
- **`portfolio/allocation.py` — pure engine** (no IBKR/DB; unit-testable). `compute_plan(targets, holdings, prices, nlv, cash, band=0.05, cash_first=True, cash_buffer=0.01)` → a `RebalancePlan` of `TradeProposal`s. Logic: per-sleeve `drift = current_wt − target_wt`; **band gate** (|drift| ≤ band → HOLD — most quarters produce no trades); holdings not in targets → SELL to 0; **cash-first** deploys idle cash (above the buffer) into the most-underweight sleeves before any SELL (tax-aware); report turnover.
- **`portfolio/rebalancer.py` — execution.** Opens one `IBKRConnection`, fetches positions/NLV/cash + reference prices (`get_last_price`), calls `compute_plan`, **dry-run by default** (two-gate: `--no-dry-run` *and* `config.allocation.rebalance_orders_enabled=True` both required), submits limit orders near mid, reconciles via `execution/reconciliation.py`, writes a `rebalance_log` row.
- **`scripts/rebalance.py`** — CLI: dry-run shows drift + plan; `--show`, `--band`, `--cash-first`, `--sleeve core|satellite`, `--no-dry-run`.

**Confirmed design decisions (2026-06-28):** (1) **fractional shares** (hit exact weights on a small book); (2) **~1% cash buffer** (rest fully invested); (3) **targets in SQLite** (`target_allocation`, dashboard-friendly); (4) **adopt a transactions/holdings + cost-basis model** for the new system — see Database Schema.

**Build order:** pure engine + tests → `target_allocation` + `set_targets.py` → `rebalance.py` dry-run (real value here, zero order risk) → gated execution + reconciliation → dashboard "Allocation" page → transactions/holdings + tax-lot harvesting (Phase 3).

## Database Schema

SQLite at `db/trading.db`; all timestamps UTC-naive. `_migrate()` in `data/database.py` runs at engine init — add a new ORM column by adding an `if "col" not in cols: ALTER TABLE` block there; never rely on `create_all()` for existing tables.

**Active (used by the new system):**
| Table | Notes |
|-------|-------|
| `ohlcv_bars` | OHLCV (+ ^VIX, SPY benchmark); unique (symbol, interval, timestamp) |
| `indicator_snapshots` | indicators recomputed from bars (used for vol/MA in any v2 overlay) |
| `fundamental_data` | yfinance fundamentals (24h cache) incl. `sector`; feeds `buffett_screen` + `data/sectors.py` |
| `fill_log` | raw IBKR executions (exec_id unique) — the audit trail from reconciliation |
| `reconciliation_state` | Phase-B watermark per source/account |
| `circuit_breaker_log` | CB TRIGGERED/RESET events |
| `equity_snapshots` | per-day NLV baseline |
| `trade_log` | closed-trade history (`source='live'` from reconciliation). Round-trip model — retained for **historical** P&L; the new system's primary ledger is the transactions/holdings model below |
| **`target_allocation`** | **NEW** — desired weights (core + satellite) |
| **`rebalance_log`** | **NEW** — one row per rebalance run |
| **`transactions` / `holdings`** | **NEW (Phase 3)** — per-fill ledger + position/cost-basis snapshots; P&L is mark-to-market vs cost basis (also feeds tax-lot harvesting). Replaces round-trip aggregation for the new buy-and-hold book |

**Historical / retired** (still in the DB, **not written by the new system**; kept for archived analysis): `signal_log`, `ensemble_weight_history`, `walk_forward_results`, `universe_assets`, `universe_run_log`, `order_decisions`, `signal_runner_log`, `trailing_stop_log`, `intraday_run_log`, `llm_news_analysis`, `news_cache`.

## Configuration

Load order: dataclass defaults (`config/settings.py`) → `config/settings.yaml` (user overrides) → env vars (secrets only: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`). Secrets are never written to YAML (`_SECRET_FIELDS`).

Relevant config: `DataConfig.watchlist` / `benchmark_symbol` ("SPY"); `TradingConfig.paper_orders_enabled` / `paper_equity` / `cash_reserve_pct`; `RiskConfig.circuit_breaker_*`; `FlexConfig.token`/`query_id`. **Planned `AllocationConfig`:** `rebalance_band=0.05`, `cash_first=True`, `cash_buffer=0.01`, `rebalance_orders_enabled=False` (the second gate), `price_source`. The retired `MLConfig` / `UniverseConfig` / `LLMConfig` fields are vestigial — referenced only by archived code and the Settings page's now-unused ML tab (a pending cleanup).

## Key Architectural Decisions (kept infrastructure)

- **SQLite over Postgres** — single-file, single-user; all timestamps UTC-naive.
- **`_migrate()` over Alembic** — idempotent `ALTER TABLE` checks at engine init.
- **asyncio event loop before `ib_insync` import** — `eventkit` calls `asyncio.get_event_loop()` at import; create/set a loop *before* `from ib_insync import …` in non-main threads (Streamlit). See `NewsClient._fetch_from_ibkr_standalone()` and `9_Account.py`.
- **`get_last_price()` 3-tier fallback** — live snapshot → 15-min delayed → yfinance. Error 10089 (no real-time subscription) is expected and triggers delayed/yfinance. Used by the rebalancer for reference prices.
- **Orders: GTC + tick-rounded** — `round(price, 2)` to satisfy IBKR minimum-tick (error 110 from float32 drift); `tif="GTC"` so orders survive outside RTH (error 10349). The rebalancer reuses this for its limit orders.
- **Informational error codes suppressed** in `IBKRConnection._on_error`: `{2104, 2106, 2107, 2119, 2158, 300, 399, 10148, 10167, 10197, 10349, 202, 10089}`.
- **Flex Web Service is the durable trade-history source; `reqExecutions` is current-session-only.** The IBKR real-time API tier returns only the current Gateway session's executions; the Gateway resets overnight, so the T+1 Flex Query Web Service (`data/flex_client.py` → `scripts/reconcile_flex.py`, same `reconcile_fills` core, dedup on `ibExecID`) is the durable backstop. Don't rely on the in-session poll across a reset.
- **`get_executions` passes an empty `ExecutionFilter()` and bounds client-side** — a server-side `time` filter is brittle (warning 2174 / silent zero rows); bound/aggregate on the normalised UTC `exec_time`. Don't re-add it.
- **`fill_log` ingestion** — `exec_id` is the sole idempotency key; only `commission`/`realized_pnl` are mutated after insert (a commissionReport can arrive on a later fetch). Reconciliation populates `fill_log`; the new system aggregates it into the transactions/holdings model (cost-basis MTM) rather than round trips.
- **Sector classification** (`data/sectors.py`, extracted from the retired `portfolio_guard`): two-tier — hardcoded `_SECTOR_MAP` (ETFs/fixtures/hand-pins) then the yfinance GICS sector in `fundamental_data`, normalised. Used by the Account page, `backfill_sectors`, and the Buffett screen.
- **Circuit breaker is shared state** — `circuit_breaker_log` in the shared DB; dashboard, scripts read/write the same state.

## Logging conventions

`core/logger.py` → one `RotatingFileHandler` to `logs/python/trading_app.log` (50 MB × 5). `trading.*` loggers default INFO; root captures WARNING+. **Level policy:** DEBUG = routine no-ops/"stored 0 rows"; INFO = real events/decisions/state changes; WARNING = recoverable/degraded; ERROR = broken state needing action. `n>0 → INFO, n==0 → DEBUG` for "stored N rows".

## Commit messages (Windows / PowerShell) — load-bearing

**Do not pass multi-line commit messages via `git commit -m @'…'@` here-strings.** This is Windows PowerShell 5.1; here-strings to a native exe are fragile — embedded quotes and tokens break argument parsing and scatter the body across `git` as bogus pathspecs. **Robust pattern:** write the message to a temp file (e.g. `.git/COMMIT_EDITMSG_CC.txt`, inside `.git/` so it's never staged) with the Write tool, `git commit -F .git/COMMIT_EDITMSG_CC.txt`, then `Remove-Item` it. Single-line `git commit -m "short msg"` (no embedded quotes) is fine. Use explicit `git add <paths>` (not `git add -A`) so unrelated working-tree edits aren't swept in.

## Testing

Unit tests mock `ib_insync`, `yfinance`, and DB calls — no live connections/network. Run `.venv/Scripts/pytest tests/ -v`. Report the count (e.g. "231 passing") in completion summaries. After schema or order-logic changes, run the full suite. New work (allocation engine) should be **pure and unit-tested first** (feed it target/holdings dicts, assert the plan) before any IBKR wiring.

## Build roadmap

1. **`portfolio/allocation.py` + tests** (pure engine) ← highest-value, lowest-risk next step.
2. `target_allocation` table + `scripts/set_targets.py` (persist the pinned core + satellite).
3. `scripts/rebalance.py` **dry-run** (drift + plan against live IBKR; no orders).
4. Gated rebalancer execution + reconciliation.
5. Dashboard "Allocation" page (target vs current, drift, plan, `rebalance_log`).
6. Transactions/holdings + cost-basis model + tax-lot harvesting (Phase 3).
7. New slow-cadence automation to replace the stale `batch_files/`.

**Pending cleanups (non-blocking):** archive `risk/{order_manager, position_sizer, portfolio_guard, trailing_stop}` + slim `risk/__init__` + split `test_risk`; remove the Settings page's vestigial ML tab + `MLConfig`/`UniverseConfig`/`LLMConfig`; archive the stale `batch_files/`.
