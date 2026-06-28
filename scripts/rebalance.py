"""Rebalance — show drift + the proposed trade plan (DRY-RUN by default).

  python scripts/rebalance.py                 # dry-run: drift + proposed plan
  python scripts/rebalance.py --band 0.05 --cash-buffer 0.01
  python scripts/rebalance.py --no-dry-run    # (Phase 2 — order submission not yet built)

Reads targets from the target_allocation table (set via scripts/set_targets.py),
fetches live IBKR positions / NLV / cash + reference prices, and runs the pure
engine (portfolio.allocation.compute_plan).  No orders are submitted — execution
is the gated Phase-2 step.  Run from the project root.
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import get_target_allocation
from portfolio.allocation import (
    SAT_BIGBET, RebalancePlan, Target, compute_plan,
)

_SLEEVE_ORDER = ["core", "satellite_qv", SAT_BIGBET]


# ── Pure assembly (unit-testable, no IBKR) ────────────────────────────────────

def targets_from_rows(rows: list[dict]) -> list[Target]:
    return [Target(ticker=r["ticker"], sleeve=r["sleeve"],
                   weight=float(r["target_weight"]), label=r.get("label") or "")
            for r in rows]


def build_plan(rows, holdings, prices, nlv, cash, *, band, cash_buffer) -> RebalancePlan:
    return compute_plan(targets_from_rows(rows), holdings, prices, nlv, cash,
                        band=band, cash_buffer=cash_buffer)


def format_plan(plan: RebalancePlan) -> str:
    out = []
    out.append(f"=== Rebalance plan (DRY-RUN) — {plan.as_of:%Y-%m-%d %H:%M} ===")
    out.append(f"NLV ${plan.nlv:,.0f}   cash ${plan.cash:,.0f}   managed ${plan.managed_nlv:,.0f}   "
               f"big-bets: held ${plan.bigbet_value:,.0f} / pending ${plan.bigbet_pending:,.0f}")
    out.append(f"band ±{plan.band*100:.0f}pp")

    by_sleeve: dict[str, list] = {}
    for p in plan.proposals:
        by_sleeve.setdefault(p.sleeve, []).append(p)
    for sleeve in _SLEEVE_ORDER + [s for s in by_sleeve if s not in _SLEEVE_ORDER]:
        props = by_sleeve.get(sleeve)
        if not props:
            continue
        out.append(f"\n[{sleeve}]")
        for p in sorted(props, key=lambda x: -x.current_wt):
            tgt = "" if sleeve == SAT_BIGBET else f" tgt {p.target_wt*100:4.1f}%"
            drift = "" if sleeve == SAT_BIGBET else f" drift {p.drift_pp:+5.1f}pp"
            out.append(f"  {p.ticker:6} cur {p.current_wt*100:5.1f}%{tgt}{drift}  {p.action:4}  {p.reason}")

    trades = plan.trades
    out.append(f"\nProposed trades ({len(trades)}):")
    if not trades:
        out.append("  none — every sleeve within band (do nothing this period).")
    for p in trades:
        out.append(f"  {p.action:4} {p.ticker:6} ${p.dollars:>9,.0f}  {p.shares:>8.2f} sh @ ${p.price:,.2f}")
    out.append(f"Turnover: {plan.turnover_pct:.1f}% of NLV")

    if plan.notes:
        out.append("\nNotes:")
        out.extend(f"  - {n}" for n in plan.notes)
    out.append("\n(dry-run — no orders submitted)")
    return "\n".join(out)


# ── IBKR fetch (live; the only part that needs Gateway) ───────────────────────

async def _fetch_state(conn):
    """Return (holdings{ticker:shares}, prices{ticker:px}, nlv, cash) from IBKR."""
    summary = await conn.get_account_summary()
    positions = await conn.get_positions()
    holdings = {p["symbol"]: p["quantity"] for p in positions}

    rows = get_target_allocation(active_only=True)
    tickers = {r["ticker"] for r in rows} | set(holdings)
    prices = {}
    for t in sorted(tickers):
        px = await conn.get_last_price(t)
        if px:
            prices[t] = px
    return rows, holdings, prices, summary.net_liquidation, summary.total_cash


async def _run(band: float, cash_buffer: float) -> int:
    from execution.ibkr_connection import IBKRConnection
    rows = get_target_allocation(active_only=True)
    if not rows:
        print("No active targets.  Run:  python scripts/set_targets.py --init-core")
        return 0

    conn = IBKRConnection()
    if not await conn.connect():
        print("IB Gateway unreachable — cannot read live positions/NLV.  "
              "(Open IB Gateway and retry.)")
        return 0
    try:
        rows, holdings, prices, nlv, cash = await _fetch_state(conn)
    finally:
        await conn.disconnect()

    plan = build_plan(rows, holdings, prices, nlv, cash, band=band, cash_buffer=cash_buffer)
    print(format_plan(plan))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Show the rebalance plan (dry-run).")
    ap.add_argument("--band", type=float, default=0.05, help="drift band (fraction; default 0.05)")
    ap.add_argument("--cash-buffer", type=float, default=0.01, help="cash buffer (fraction; default 0.01)")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="(Phase 2) submit orders — NOT yet implemented; stays dry")
    args = ap.parse_args()
    if args.no_dry_run:
        print("Order submission is Phase 2 (gated by config.allocation.rebalance_orders_enabled) "
              "and not yet implemented — showing the dry-run plan only.\n")
    raise SystemExit(asyncio.run(_run(args.band, args.cash_buffer)))


if __name__ == "__main__":
    main()
