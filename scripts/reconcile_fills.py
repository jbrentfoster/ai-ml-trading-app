"""
Standalone fill-reconciliation CLI (Phase B).

Thin argparse wrapper over ``execution.reconciliation.reconcile_fills`` — the
*same* core the daily signal_runner Phase 1 hook calls, so there is exactly one
copy of the reconciliation logic.  This script exists for two purposes:

  1. **Backfill** off-cycle fills that fired between daily runs (bracket STP /
     LMT / TRAIL legs).  IBKR retains ~7 days of execution history server-side,
     so run this within a week of a fill to capture it before it ages out.
  2. **Ad-hoc debugging** of the reconciler against a custom window or symbol.

Usage:
    python scripts/reconcile_fills.py                 # full available window
    python scripts/reconcile_fills.py --since 2026-05-20   # explicit window start
    python scripts/reconcile_fills.py --dry-run       # print would-be writes, write nothing
    python scripts/reconcile_fills.py --symbol SPY    # single-symbol reconciliation

Requires IB Gateway running.  On Gateway-down it prints a marked line and exits
non-zero (unlike the intraday runner, this is an interactive/manual tool — a
failed connect should be visible, not silently swallowed).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import get_logger

log = get_logger("reconcile_fills")


def _force_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 with replace-on-error (Windows shells)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _setup_loop_and_connect():
    """Open one IBKR connection bound to a fresh event loop.

    ib_insync's IB client grabs the current-thread loop during IB()
    construction, so the loop must be set BEFORE IBKRConnection() is
    instantiated.  Returns (ibkr, loop) on success, (None, None) on failure.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from execution.ibkr_connection import IBKRConnection
        ibkr = IBKRConnection()
        connected = loop.run_until_complete(ibkr.connect())
        if not connected:
            try:
                loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(None)
            return None, None
        return ibkr, loop
    except Exception as exc:
        log.warning("IBKR connect raised: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)
        return None, None


def _teardown(ibkr, loop) -> None:
    if ibkr is not None and loop is not None:
        try:
            loop.run_until_complete(ibkr.disconnect())
        except Exception as exc:
            log.warning("IBKR disconnect error: %s", exc)
    if loop is not None:
        try:
            loop.close()
        except Exception:
            pass
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


def main() -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="Reconcile IBKR fills → trade_log")
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        default="",
        help="Window start (default: last reconciled watermark, else 7 days ago)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the DB",
    )
    parser.add_argument(
        "--symbol",
        default="",
        metavar="SYM",
        help="Reconcile a single symbol only (debugging)",
    )
    args = parser.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}")
            return 2

    print("=== Fill reconciliation ===")
    if args.dry_run:
        print("  (dry-run — no DB writes)")

    ibkr, loop = _setup_loop_and_connect()
    if ibkr is None or loop is None:
        print("  IBKR unreachable — is IB Gateway running? Aborting.")
        return 1

    try:
        from execution.reconciliation import reconcile_fills
        result = reconcile_fills(
            lambda s: loop.run_until_complete(ibkr.get_executions(s)),
            since=since,
            symbol=(args.symbol.upper() or None),
            dry_run=args.dry_run,
        )
        print(
            f"  Window start:   {result.window_start}\n"
            f"  New fills:      {result.n_new_fills}\n"
            f"  Cost-updated:   {result.n_cost_updated}\n"
            f"  Skipped fills:  {result.n_skipped_fills}\n"
            f"  Trades written: {result.n_trades_written}\n"
            f"  Trades skipped: {result.n_trades_skipped} (dedup)\n"
            f"  Deferred:       {result.n_deferred_cost} (commission pending)\n"
            f"  Orphans:        {result.n_orphans}"
        )
        return 0
    except Exception as exc:
        log.error("Reconciliation failed: %s", exc)
        print(f"  Reconciliation failed — {exc}")
        return 1
    finally:
        _teardown(ibkr, loop)


if __name__ == "__main__":
    raise SystemExit(main())
