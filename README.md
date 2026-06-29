> ⚠️ **For educational and paper-trading use only. Not financial advice.** See [Disclaimer](#disclaimer).

# Risk-Premia Harvesting Portfolio Tool

A personal, slow-cadence portfolio tool that harvests the **value + quality** risk
premium on a small capital sleeve, running on Interactive Brokers via IB Gateway.
It holds a diversified, value+quality-tilted **ETF core (80%)** plus a small,
concentrated Buffett-style **stock satellite (20%)**, and rebalances on a slow
(≈ quarterly) cadence with ruthless cost/tax/risk discipline. Built as a
learning / science project (and a possible legacy tool), **not** as the operator's
primary wealth.

It is explicitly **not** a return-chasing alpha machine. Success is *risk-adjusted*
(Sharpe, max drawdown, Calmar) versus a 60/40 benchmark and durability across
multi-year value droughts — **not** beating SPY total return (it will lag SPY in
growth bulls, by design — that is the cost of diversification + discipline).

> ### This project pivoted (2026-06)
> It began as a **predictive-alpha** system (an LSTM + XGBoost + FinBERT ensemble +
> an LLM news analyst trading daily signals). Four predictive-alpha directions were
> each tested with cheap probes and **retired on evidence** — durable alpha from
> commodity public data on a laptop kept not being there. The predictive layer is
> **archived, not deleted** (git tag `v1.0-predictive-alpha`, code under
> [`archive/`](archive/README.md), docs under [`archive/docs/`](archive/docs/README.md)).
>
> **Why:** [`docs/strategy/pivot_decision_2026-06.md`](docs/strategy/pivot_decision_2026-06.md) ·
> **Strategy:** [`docs/strategy/risk_premia_harvesting.md`](docs/strategy/risk_premia_harvesting.md)

---

## How it works

```
set_targets.py   → target_allocation table   (desired weights: core + satellite)
rebalance.py     → compares targets to the LIVE IBKR account
                 → a plan (drift + proposed trades), DRY-RUN by default
[two gates]      → submits marketable-limit orders, logs the run
reconcile_*.py   → fills land in fill_log
dashboard Page 3 → holdings (cost-basis P&L), drift, rebalance history
```

- **Pure allocation engine** (`portfolio/allocation.py`) — unit-tested, no IBKR/DB.
  Band-based rebalancing (no churn for small drift); idle cash deployed to
  underweight sleeves; fractional shares; **big-bets are drift-exempt** (winners run
  untouched).
- **Two-gate execution** — orders submit only with `--no-dry-run` *and*
  `config.allocation.rebalance_orders_enabled=True`. Off by default.
- **Cost-basis holdings** reconstructed from reconciled fills; surfaced on the
  Streamlit dashboard.

See [`docs/operating_guide.md`](docs/operating_guide.md) for the day-to-day runbook.

---

## Setup

```bash
pip install -r requirements.txt
```

Run all commands from the project root. Activate `.venv/` or prefix with
`.venv/Scripts/python` on Windows.

### IB Gateway

Used for order submission and live account/price reads (the data pipeline works
without it). Download from **ibkr.com → Trading → Trading Software → IB Gateway**.

```
Log in with paper-trading credentials
Configure → Settings → API → Settings
  ☑ Enable ActiveX and Socket Clients
  Socket port: 4002          ← paper (4001 = live)
  ☐ Read-Only API            ← uncheck to allow orders
Configure → Settings → Auto-restart
  ☑ Auto-restart             ← handles the daily session reset
```

---

## Commands

```bash
# Set desired weights → target_allocation
python scripts/set_targets.py --init-core        # pinned ETF core (80%)
python scripts/set_targets.py --qv "AAPL:0.05,KO:0.05,JNJ:0.05"   # quality-value sat
python scripts/set_targets.py --bigbet "TICK:0.025"               # capped conviction bet
python scripts/set_targets.py --show

# Rebalance
python scripts/rebalance.py                      # dry-run: drift + proposed plan
python scripts/rebalance.py --no-dry-run         # submit (ALSO needs the config gate)

# Data + screen
python scripts/run_pipeline.py                   # OHLCV + fundamentals (+ news)
python scripts/buffett_screen.py                 # ranked large-cap satellite shortlist

# Reconcile fills → fill_log
python scripts/reconcile_flex.py                 # durable T+1 Flex backstop
python scripts/reconcile_fills.py                # in-session poll

# Dashboard + tests
streamlit run dashboard/1_Market_Data.py         # Page 3 = Allocation
.venv/Scripts/pytest tests/ -v
```

---

## Project layout

| Path | What |
|---|---|
| `portfolio/` | allocation engine (pure) + rebalancer (gated execution) |
| `scripts/` | `set_targets.py`, `rebalance.py`, `buffett_screen.py`, pipeline + reconcile |
| `data/` | SQLite ORM + migrations, fetcher, fundamentals, sectors, UI queries |
| `execution/` | IBKR async connection + fill reconciliation |
| `dashboard/` | Streamlit pages (Page 3 = Allocation) |
| `docs/` | [strategy](docs/strategy/), [operating guide](docs/operating_guide.md), [findings + case studies](docs/README.md) (the science record) |
| `archive/` | retired predictive-alpha code + docs (tag `v1.0-predictive-alpha`) |
| `CLAUDE.md` | build/architecture reference |

---

## Requirements

- Python 3.11+
- IB Gateway 10.x (for execution + live reads; the data pipeline runs without it)
- Windows, macOS, or Linux

---

## Disclaimer

This software is provided for educational and paper-trading purposes only. It is not
financial or investment advice. The authors make no guarantees regarding
performance, accuracy, or fitness for any particular purpose. Use in live or
real-money trading is entirely at your own risk. The authors are not liable for any
financial losses, damages, or other consequences arising from use of this software.
