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


def _log_run(plan, results) -> None:
    import uuid
    from data.database import log_rebalance
    log_rebalance({
        "run_id":       str(uuid.uuid4()),
        "run_at":       plan.as_of,
        "mode":         "live",
        "nlv":          plan.nlv,
        "n_proposed":   len(plan.trades),
        "n_submitted":  sum(1 for r in results if r.ok),
        "n_failed":     sum(1 for r in results if r.status.startswith("FAILED")),
        "turnover_pct": plan.turnover_pct,
        "notes":        ("; ".join(plan.notes)[:500] or None) if plan.notes else None,
    })


async def _run(band: float, cash_buffer: float, no_dry_run: bool) -> int:
    from execution.ibkr_connection import IBKRConnection
    from config.settings import config
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
        plan = build_plan(rows, holdings, prices, nlv, cash, band=band, cash_buffer=cash_buffer)
        print(format_plan(plan))

        if not no_dry_run:
            return 0
        # ── Gate 2: config flag must also be armed ──
        if not config.allocation.rebalance_orders_enabled:
            print("\n--no-dry-run given, but config.allocation.rebalance_orders_enabled is False — "
                  "NOT submitting (the second gate is off).  Set it True to arm execution.")
            return 0
        if not plan.trades:
            print("\nNo trades to submit.")
            return 0

        from portfolio.rebalancer import submit_plan
        print(f"\n=== SUBMITTING {len(plan.trades)} order(s) (LIVE) ===")
        results = await submit_plan(
            conn, plan,
            slippage_cap=config.allocation.slippage_cap,
            share_precision=config.allocation.share_precision)
        for r in results:
            print(f"  {r.action:4} {r.ticker:6} x{r.shares:<10.4f} @ ${r.limit_price:>9,.2f}  -> {r.status}")
        _log_run(plan, results)
        n_ok = sum(1 for r in results if r.ok)
        print(f"\nSubmitted {n_ok}/{len(results)} order(s).  Fills reconcile via "
              "scripts/reconcile_flex.py (T+1) / reconcile_fills.py.")
    finally:
        await conn.disconnect()
    return 0


def main() -> None:
    from config.settings import config
    ap = argparse.ArgumentParser(description="Show / execute the rebalance plan.")
    ap.add_argument("--band", type=float, default=config.allocation.rebalance_band,
                    help="drift band (fraction; default from config)")
    ap.add_argument("--cash-buffer", type=float, default=config.allocation.cash_buffer,
                    help="cash buffer (fraction; default from config)")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="submit orders (ALSO requires config.allocation.rebalance_orders_enabled)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_run(args.band, args.cash_buffer, args.no_dry_run)))


if __name__ == "__main__":
    main()
