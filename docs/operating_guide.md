# Operating guide — how to run the risk-premia book

The human runbook. For *what* the strategy is and *why*, read
[`strategy/risk_premia_harvesting.md`](strategy/risk_premia_harvesting.md); for the
code/architecture, read [`../CLAUDE.md`](../CLAUDE.md). This doc is just the
day-to-day operating procedure.

## The model in one breath

```
set_targets.py        → target_allocation table   (what you WANT to hold)
rebalance.py          → compares targets to your LIVE IBKR account
                      → a plan (drift + proposed trades), DRY-RUN by default
[arm both gates]      → submits marketable-limit orders
reconcile_flex.py     → fills land in fill_log
dashboard Page 3      → holdings, drift, P&L, rebalance history (reads SQLite)
```

Prereqs: activate `.venv` (or prefix `.venv/Scripts/python`), run everything from
the project root, and have **IB Gateway** running (paper port 4002) for any command
that reads the live account or submits orders.

---

## The two execution gates (read this first)

Orders are submitted **only** when *both* are true:

1. you pass `--no-dry-run` on `rebalance.py`, **and**
2. `config.allocation.rebalance_orders_enabled = True` (in `config/settings.yaml`).

With either gate off, `rebalance.py` prints the plan and submits nothing. Keep gate
2 **off** by default; flip it on only for the moment you execute, then flip it back.
This is deliberate — nothing can move money by accident.

---

## Workflow A — initial deployment (one-time)

1. **Seed the core** (the pinned ETF allocation):
   ```bash
   python scripts/set_targets.py --init-core
   ```
2. **Seed the satellite** (your judgment, from the screen):
   ```bash
   python scripts/buffett_screen.py            # ranked large-cap shortlist
   python scripts/set_targets.py --qv "TICK:0.05,TICK:0.05,TICK:0.05"
   # optional conviction big-bet (caps enforced: <=3%/name, <=5% total):
   python scripts/set_targets.py --bigbet "TICK:0.025"
   python scripts/set_targets.py --show         # confirm sleeves + cap checks
   ```
3. **Fetch prices** so the new tickers have bars for the dashboard:
   ```bash
   python scripts/run_pipeline.py --skip-news
   ```
4. **Dry-run and review** — on a fresh account this proposes selling any leftover
   positions (flagged `untracked`) and buying the core/satellite to target:
   ```bash
   python scripts/rebalance.py
   ```
5. **Arm and execute** — set `allocation.rebalance_orders_enabled: true` in
   `config/settings.yaml`, then:
   ```bash
   python scripts/rebalance.py --no-dry-run
   ```
   …then set it back to `false`.
6. **Reconcile** the fills:
   ```bash
   python scripts/reconcile_flex.py            # T+1 durable (no Gateway needed)
   # or, same session as the fills:  python scripts/reconcile_fills.py
   ```
7. **Watch** — `streamlit run dashboard/1_Market_Data.py` → **Page 3 Allocation**.

---

## Workflow B — quarterly core rebalance

The whole point of band-based rebalancing: **most quarters you do nothing.**

```bash
python scripts/rebalance.py                    # dry-run
```
- If every sleeve is within the band (`±5pp` default) → the plan is empty → **stop,
  do nothing.**
- If a sleeve has drifted past the band → review the proposed trades, then arm both
  gates and `--no-dry-run`, then reconcile (steps 5–6 above).

Run it during market hours so the marketable-limit orders fill.

---

## Workflow C — periodic satellite refresh

When you want to re-screen the quality-value names or adjust a big-bet:

```bash
python scripts/buffett_screen.py
python scripts/set_targets.py --qv "NEW:0.05,NEW:0.05,..."   # replaces the qv sleeve
```
Then dry-run → execute as in Workflow B. The old satellite rows are kept as history
(`active=False`); the rebalancer will sell the dropped names and buy the new ones.

**Big-bets are different.** They are **drift-exempt**: the rebalancer never trims a
winner or tops up a loser. To *open* one, add a target (`--bigbet`) and it gets a
one-time entry buy to its cap; to *close/replace* a dead one, remove its target and
add a new name. See strategy doc §4 (the entry-cap-not-target rule + the windfall
threshold). Never average down.

---

## Config knobs (`config/settings.yaml` → `allocation:`)

| Key | Default | Meaning |
|---|---|---|
| `rebalance_band` | `0.05` | per-sleeve drift band; below this → HOLD (no churn) |
| `cash_buffer` | `0.01` | fraction of NLV held back as cash |
| `rebalance_orders_enabled` | `false` | **gate 2** — must be true to submit |
| `slippage_cap` | `0.005` | marketable-limit offset from the reference price |
| `share_precision` | `4` | fractional-share rounding (0 = whole shares) |

CLI overrides: `rebalance.py --band 0.07 --cash-buffer 0.02`.

---

## Reading Page 3 (Allocation)

- **Target vs current weight** — grey = target, teal = current (% of invested
  capital). Tickers at 0% current aren't held yet; `untracked` names are positions
  to sell into the targets.
- **Drift table** — colour-graded; the live cash-aware drift/plan is `rebalance.py`.
- **Holdings** — average-cost basis from `fill_log`, valued at the latest cached
  close (refresh with `run_pipeline.py` / `refresh_recent_bars.py`).
- **Rebalance history** — one row per *live* run (dry-runs aren't logged).

## Cadence summary

| Cadence | Action |
|---|---|
| Quarterly | `rebalance.py` dry-run; act only if a sleeve breached the band |
| When you re-screen | `buffett_screen.py` → `set_targets.py --qv` → rebalance |
| Opportunistic | open/replace a `--bigbet` (capped, drift-exempt) |
| After any execution | `reconcile_flex.py`, then glance at Page 3 |
