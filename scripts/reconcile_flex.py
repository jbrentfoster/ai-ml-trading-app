"""Daily Flex reconciliation — durable backstop for between-run live fills.

Fetches the configured IBKR Flex Query (Trades, Level of Detail = Execution) via
the Flex Web Service, parses it with the SAME parser the one-time backfill uses
(``scripts/backfill_flex_trades.py:parse_flex_trades``), and feeds it through the
SAME ``execution.reconciliation.reconcile_fills`` core the live poll uses.  Dedup
is keyed on ``ibExecID`` / ``exit_exec_id``, so overlapping with fills the
in-session ``reqExecutions`` poll already captured is a no-op.

WHY: ``reqExecutions`` only sees the current Gateway session, which resets
overnight, so the morning poll misses every between-run fill (2026-06-08/09
escalation — IWM/SATS entries never reached fill_log).  Flex retains a year+ and
is session-independent, so this step recovers them the morning after.  Flex is
**T+1** (today's fills appear tomorrow), so this is the durable backstop; the
in-session poll remains the same-day path.

No IB Gateway required — this is a pure HTTPS call to IBKR's Flex service.

No-op (exit 0) when ``config.flex`` is not configured (no ``IBKR_FLEX_TOKEN`` /
``IBKR_FLEX_QUERY_ID``) so the run_daily.bat step is safe before credentials are
set.  A Flex service error (throttle exhausted, generation failure) also logs +
exits 0 — same graceful-degradation contract as the Gateway phases; tomorrow's
run picks up anything missed.

Usage:
    python scripts/reconcile_flex.py
    python scripts/reconcile_flex.py --dry-run        # fetch + parse + show, no writes
    python scripts/reconcile_flex.py --symbol IWM     # one symbol only
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from core.logger import get_logger
from data.flex_client import FlexError, fetch_flex_statement
from execution.reconciliation import reconcile_fills
from scripts.backfill_flex_trades import parse_flex_trades

log = get_logger("scripts.reconcile_flex")


def _force_utf8_streams() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main() -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="Daily IBKR Flex trade reconciliation")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + parse + show would-be writes; touch no tables")
    parser.add_argument("--symbol", default="", metavar="SYM",
                        help="Reconcile a single symbol only")
    args = parser.parse_args()

    print("=== Flex reconciliation ===")
    if not config.flex.enabled:
        print("  Flex not configured (set IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID) — skipping.")
        return 0
    if args.dry_run:
        print("  (dry-run — no DB writes)")

    # Fetch — degrade gracefully on any Flex service error (throttle / generation
    # failure).  Exit 0 so the daily batch continues; tomorrow's run retries.
    try:
        xml_text = fetch_flex_statement(config.flex.token, config.flex.query_id)
    except FlexError as exc:
        log.warning("Flex fetch failed — skipping reconciliation this run: %s", exc)
        print(f"  ⚠  Flex fetch failed: {exc}")
        print("     Skipping (exit 0); tomorrow's run will pick up anything missed.")
        return 0

    try:
        executions = parse_flex_trades(xml_text, source_tz=(config.flex.source_tz or None))
    except Exception as exc:
        log.warning("Flex XML parse failed — skipping: %s", exc)
        print(f"  ⚠  Flex XML parse failed: {exc}")
        return 0

    if not executions:
        print("  Flex statement had 0 execution rows (empty window). Nothing to do.")
        return 0

    print(f"  Parsed {len(executions)} execution row(s) from Flex statement")

    # Reuse the exact reconcile core.  live_positions=None (default): the Flex
    # path has no broker-position state and is itself the recovery mechanism, so
    # net>0 orphans are treated as still-open (legacy behaviour) rather than
    # flagged as missed exits — the same stance backfill_flex_trades.py takes.
    result = reconcile_fills(
        lambda _since: executions,
        since=datetime(2000, 1, 1),
        symbol=(args.symbol.upper() or None),
        dry_run=args.dry_run,
    )

    print(
        f"  New fills:      {result.n_new_fills}\n"
        f"  Cost-updated:   {result.n_cost_updated}\n"
        f"  Skipped fills:  {result.n_skipped_fills} (already ingested)\n"
        f"  Trades written: {result.n_trades_written}\n"
        f"  Trades skipped: {result.n_trades_skipped} (dedup — already reconciled)\n"
        f"  Orphans:        {result.n_orphans} (entry with no matching exit in window)"
    )
    if result.n_trades_written:
        print(f"  ✓  {result.n_trades_written} live round trip(s) recovered via Flex.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
