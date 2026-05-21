"""
Intraday lightweight runner — Phase 1 + Phase 3.5 only.

Scheduled at 12:00 ET and 15:30 ET on weekdays via Windows Task Scheduler
(see run_intraday.bat).  Does NOT replace scripts/signal_runner.py at 09:35 ET —
that runner remains the canonical daily signal + order + trailing-stop pipeline.
This script exists for the two things that genuinely benefit from intraday
re-evaluation:

  * Phase 1 — circuit breaker check.  Pulls NLV from IBKR, compares against the
              equity_snapshots baseline (written by the morning daily runner),
              auto-trips the breaker on threshold breach.  When tripped AND
              not in dry-run AND paper_orders_enabled=True, flattens all longs
              (cancels bracket children including TRAIL, then market sells).

  * Phase 3.5 — trailing-stop re-evaluation with the **live IBKR price**
                (NOT the cached daily close).  By default this is ratchet-only
                (RATCHETED rows logged, no new conversions).  Opt-in mid-day
                conversions are gated by config.risk.intraday_trail_conversion_enabled
                AND a buffer above the daily activation threshold (intraday
                operates against the same daily ATR, so anything close to
                activation by noon was *just* checked at 09:35).

What this script explicitly DOES NOT do:
  * Signal regeneration (LSTM/XGB/FinBERT scores stay on the daily cadence).
  * Data refresh (no mid-day yfinance fetch — those bars are partial and noisy).
  * News fetch / fundamentals refresh / model retraining / universe rescore.
  * Hold-timeout (Phase 3.6) — calendar-day based, no intraday benefit.
  * Writes to signal_log or signal_runner_log — its own intraday_run_log table.

Gateway-down failure mode:
  IBKR Gateway logs out overnight on this account.  If the gateway is
  unreachable at run time, this script writes a status='gateway_down' row
  to intraday_run_log, logs a WARNING, prints a marked stdout line, and
  exits with code 0.  This avoids Windows Task Scheduler treating the run
  as a failure and retry-storming an already-flaky gateway, while keeping
  the missed run visible on Page 8 the next morning.

Usage:
    python scripts/intraday_check.py            # dry-run (default — safe)
    python scripts/intraday_check.py --dry-run  # explicit dry-run
    python scripts/intraday_check.py --no-dry-run  # enable CB flatten + trail conversion paths
    python scripts/intraday_check.py --symbol AAPL  # debug a single position
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config, TradingMode
from core.logger import get_logger
from data.database import (
    get_equity_snapshot_on_or_before,
    log_intraday_run,
)
from risk.circuit_breaker import CircuitBreaker
from risk.order_manager import flatten_all_longs
from risk.trailing_stop import TrailingStopManager

log = get_logger("intraday_check")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _setup_loop_and_connect():
    """Open one IBKR connection bound to a fresh event loop.

    Mirrors signal_runner._connect_ibkr_if_needed: ib_insync's IB client grabs
    the current-thread loop during IB() construction, so the loop must be set
    BEFORE IBKRConnection() is instantiated.  Returns (ibkr, loop) on success,
    or (None, None) on any failure — caller handles the gateway-down path.
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
        log.warning("Intraday IBKR connect raised: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)
        return None, None


def _teardown(ibkr, loop) -> None:
    """Close IBKR + event loop in a finally block; swallow per-step errors."""
    if ibkr is not None and loop is not None:
        try:
            loop.run_until_complete(ibkr.disconnect())
        except Exception as exc:
            log.warning("Intraday IBKR disconnect error: %s", exc)
    if loop is not None:
        try:
            loop.close()
        except Exception:
            pass
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


def _make_price_source(ibkr, loop):
    """Build the sync price-source shim for TrailingStopManager.

    The manager's price_source contract is sync (symbol → float), but IBKR's
    get_last_price is async.  This shim drives the same loop the IB client
    is bound to, returns 0.0 on None / raise (treated as "no valid price"
    by the manager → emits a SKIPPED row).
    """
    def _source(symbol: str) -> float:
        try:
            price = loop.run_until_complete(ibkr.get_last_price(symbol))
        except Exception as exc:
            log.warning("get_last_price raised for %s: %s", symbol, exc)
            return 0.0
        if price is None:
            log.info("get_last_price returned None for %s — SKIPPED downstream", symbol)
            return 0.0
        return float(price)
    return _source


# ── Phase implementations ────────────────────────────────────────────────────

def _phase1_circuit_breaker(ibkr, loop, dry_run: bool, run_id: str) -> dict:
    """Pull NLV, compute loss-pct against baselines, run CB check, flatten if tripped.

    Returns a dict with keys: daily_loss_pct, weekly_loss_pct, cb_tripped,
    positions_flattened, error.  Any field may be None when the CB check
    could not be performed (e.g. no baseline yet on a fresh DB).

    The intraday runner does NOT write equity_snapshots — that's the daily
    runner's job at 09:35 ET.  Writing here would overwrite the morning
    baseline that this very check is comparing against.
    """
    result = {
        "daily_loss_pct":      None,
        "weekly_loss_pct":     None,
        "cb_tripped":          None,
        "positions_flattened": 0,
        "error":               None,
    }
    cb = CircuitBreaker()

    # If the breaker is already halted from a prior trigger, skip the auto-check
    # but still report the state.
    halted, reason = cb.is_halted()
    if halted:
        log.info("Intraday CB check: breaker already halted (%s)", reason)
        result["cb_tripped"] = 1
        # Skip flatten on an already-halted breaker — the original trigger has
        # presumably already done its work.  Operators reset via Page 8 once
        # they've confirmed positions are clean.
        return result

    try:
        summary = loop.run_until_complete(ibkr.get_account_summary())
    except Exception as exc:
        log.warning("Intraday CB check: get_account_summary failed: %s", exc)
        result["error"] = f"get_account_summary failed: {exc}"
        return result

    nlv = float(summary.net_liquidation or 0.0)
    if nlv <= 0:
        log.warning("Intraday CB check: NLV <= 0, skipping comparison")
        result["error"] = "NLV <= 0"
        return result

    today = _now().date()
    daily_base = get_equity_snapshot_on_or_before(
        (today - timedelta(days=1)).strftime("%Y-%m-%d")
    )
    weekly_base = get_equity_snapshot_on_or_before(
        (today - timedelta(days=7)).strftime("%Y-%m-%d")
    )
    if daily_base is None and weekly_base is None:
        log.info("Intraday CB check: no prior equity baseline — skipping comparison")
        return result  # cb_tripped stays None

    daily_pct = 0.0
    weekly_pct = 0.0
    if daily_base and daily_base["net_liquidation"]:
        daily_pct = (nlv - daily_base["net_liquidation"]) / daily_base["net_liquidation"]
    if weekly_base and weekly_base["net_liquidation"]:
        weekly_pct = (nlv - weekly_base["net_liquidation"]) / weekly_base["net_liquidation"]

    result["daily_loss_pct"]  = daily_pct
    result["weekly_loss_pct"] = weekly_pct

    print(
        f"  Equity: NLV ${nlv:,.2f}  |  "
        f"Daily Δ {daily_pct:+.2%}  |  Weekly Δ {weekly_pct:+.2%}"
    )

    triggered = cb.check_loss_limits(daily_pct, weekly_pct)
    result["cb_tripped"] = 1 if triggered else 0

    if triggered:
        log.warning(
            "Intraday CB auto-tripped: daily=%.4f weekly=%.4f", daily_pct, weekly_pct,
        )
        print(f"  🔴 CIRCUIT BREAKER AUTO-TRIPPED — daily {daily_pct:+.2%}, weekly {weekly_pct:+.2%}")

        # Flatten longs only when we have authority to mutate the account.
        paper_ok = (
            config.trading.mode == TradingMode.LIVE
            or (
                config.trading.mode == TradingMode.SIMULATION
                and config.trading.paper_orders_enabled
            )
        )
        if not dry_run and paper_ok:
            print("  Flattening all long positions...")
            flattened = flatten_all_longs(ibkr, loop, run_id=run_id)
            result["positions_flattened"] = flattened
            print(f"  Flattened {flattened} position(s).")
        else:
            print(
                "  (dry-run or paper_orders_enabled=False — flatten skipped; "
                "CB row still written.)"
            )

    return result


def _phase3_5_intraday_trail(ibkr, loop, dry_run: bool, run_id: str) -> dict:
    """Re-evaluate trailing stops against live IBKR price.

    Skipped entirely when trailing_stop_enabled=False (matches daily Phase 3.5).
    In dry-run, the manager still runs but conversions are blocked by both
    (a) the intraday_trail_conversion_enabled config gate AND
    (b) the daily-Phase-3.5 dry-run convention (the manager itself never
        gates on dry-run — the runner controls that by simply not invoking
        the conversion path; the intraday runner achieves the same by passing
        intraday=True which engages the ratchet-only short-circuit when
        intraday_trail_conversion_enabled=False).

    Returns counts dict: evaluated, ratcheted, converted.
    """
    result = {"evaluated": 0, "ratcheted": 0, "converted": 0}

    if not config.risk.trailing_stop_enabled:
        log.debug("Intraday trail: trailing_stop_enabled=False — skipping")
        return result

    # In dry-run mode, force the manager into ratchet-only by intercepting
    # the config flag.  The manager already does this when
    # intraday_trail_conversion_enabled=False, but we want dry-run to be
    # absolutely guaranteed to never submit orders regardless of YAML state.
    # Cleanest way: pass intraday=True and let the manager's own gate work
    # if the config flag is False; if the config flag is True but we're
    # dry-running, we still need a second layer of defence.  Implemented via
    # the dry-run override below.
    price_source = _make_price_source(ibkr, loop)
    manager = TrailingStopManager(ibkr_connection=ibkr, event_loop=loop)

    # Dry-run safety net: temporarily force the config flag off for this
    # invocation so the manager's gate suppresses conversions even if the
    # operator has flipped intraday_trail_conversion_enabled=True in YAML.
    # The flag is restored in finally.
    saved_flag = config.risk.intraday_trail_conversion_enabled
    if dry_run:
        config.risk.intraday_trail_conversion_enabled = False

    try:
        actions = manager.manage(
            run_id=run_id,
            price_source=price_source,
            intraday=True,
        )
    finally:
        config.risk.intraday_trail_conversion_enabled = saved_flag

    result["evaluated"] = len(actions)
    for act in actions:
        if act.action == "RATCHETED":
            result["ratcheted"] += 1
            print(
                f"  {act.symbol}: RATCHETED — {act.reason}"
            )
        elif act.action == "CONVERTED":
            result["converted"] += 1
            print(
                f"  {act.symbol}: CONVERTED — entry={act.entry_price:.2f} "
                f"current={act.current_price:.2f} trail=${act.trail_amount:.2f}"
            )
        elif act.action == "FAILED":
            print(f"  {act.symbol}: FAILED — {act.reason}")
        else:
            # SKIPPED — print compactly so noisy positions don't dominate.
            print(f"  {act.symbol}: skipped ({act.reason})")

    return result


# ── Main entry ───────────────────────────────────────────────────────────────

def run(dry_run: bool = True, symbol_filter: str = "") -> int:
    """Execute one intraday check.  Returns OS exit code (always 0 — see docstring)."""
    run_id        = str(uuid.uuid4())
    t_start       = time.monotonic()
    started_at    = _now()
    status        = "completed"
    error_message: str | None = None

    daily_pct: float | None = None
    weekly_pct: float | None = None
    cb_tripped: int | None  = None
    positions_flattened     = 0
    trailing_evaluated      = 0
    trailing_ratcheted      = 0
    trailing_converted      = 0

    if dry_run:
        mode_label = "DRY_RUN (--dry-run flag)"
    elif (
        config.trading.mode == TradingMode.SIMULATION
        and not config.trading.paper_orders_enabled
    ):
        mode_label = "DRY_RUN (paper_orders_enabled=False)"
    elif config.trading.mode == TradingMode.SIMULATION:
        mode_label = "PAPER"
    else:
        mode_label = "LIVE"

    print(f"\n{'=' * 60}")
    print(f"  Intraday Check — {started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode: {mode_label} | run_id={run_id[:8]}")
    print(f"{'=' * 60}\n")

    # ── Connect ──────────────────────────────────────────────────────────────
    print("=== Phase 0: IBKR connect ===")
    ibkr, loop = _setup_loop_and_connect()
    if ibkr is None or loop is None:
        status = "gateway_down"
        error_message = "IBKR connect failed or raised"
        print(
            f"  ⚠  Gateway unreachable at {datetime.now().strftime('%H:%M:%S')} "
            "— intraday check skipped"
        )
        log.warning("Intraday check skipped: IBKR gateway unreachable.")

        # Persist the gateway-down row BEFORE returning so the missed check
        # is visible on Page 8.  Wrapped in its own try so a DB failure
        # doesn't escape and trip Task Scheduler.
        try:
            log_intraday_run({
                "run_id":              run_id,
                "run_timestamp":       started_at,
                "mode":                "intraday",
                "status":              status,
                "daily_loss_pct":      None,
                "weekly_loss_pct":     None,
                "cb_tripped":          None,
                "positions_flattened": 0,
                "trailing_evaluated":  0,
                "trailing_ratcheted":  0,
                "trailing_converted":  0,
                "duration_seconds":    time.monotonic() - t_start,
                "error_message":       error_message,
            })
        except Exception as exc:
            log.error("Could not persist gateway_down row: %s", exc)
        print()
        print("=== Done (gateway_down) ===\n")
        return 0
    print("  ✓ IBKR connected.")
    print()

    # ── Phase 1: Circuit breaker ────────────────────────────────────────────
    print("=== Phase 1: Circuit breaker check ===")
    try:
        ph1 = _phase1_circuit_breaker(ibkr, loop, dry_run=dry_run, run_id=run_id)
        daily_pct           = ph1["daily_loss_pct"]
        weekly_pct          = ph1["weekly_loss_pct"]
        cb_tripped          = ph1["cb_tripped"]
        positions_flattened = ph1["positions_flattened"]
        if ph1["error"]:
            error_message = ph1["error"]
    except Exception as exc:
        log.error("Phase 1 raised: %s", exc, exc_info=True)
        error_message = f"phase1 raised: {exc}"
    print()

    # If CB tripped, mark status accordingly and skip Phase 3.5 — flattening
    # has already happened (or been intentionally suppressed) and re-evaluating
    # trailing stops after a wholesale flatten is meaningless.
    if cb_tripped == 1:
        status = "cb_tripped"
    else:
        # ── Phase 3.5: Intraday trailing-stop re-evaluation ──────────────────
        print("=== Phase 3.5: Intraday trailing stops ===")
        try:
            # Symbol filter is informational only here — the manager walks every
            # long position regardless.  Logged for traceability.
            if symbol_filter:
                print(f"  (symbol filter '{symbol_filter}' is informational; "
                      "manager evaluates all longs)")
            ph35 = _phase3_5_intraday_trail(ibkr, loop, dry_run=dry_run, run_id=run_id)
            trailing_evaluated = ph35["evaluated"]
            trailing_ratcheted = ph35["ratcheted"]
            trailing_converted = ph35["converted"]
            if trailing_evaluated == 0:
                print("  No long positions to evaluate.")
        except Exception as exc:
            log.error("Phase 3.5 raised: %s", exc, exc_info=True)
            error_message = (error_message or "") + f" | phase35 raised: {exc}"
            status = "error"
        print()

    # ── Teardown ─────────────────────────────────────────────────────────────
    _teardown(ibkr, loop)

    duration = time.monotonic() - t_start

    # ── Persist run summary ──────────────────────────────────────────────────
    try:
        log_intraday_run({
            "run_id":              run_id,
            "run_timestamp":       started_at,
            "mode":                "intraday",
            "status":              status,
            "daily_loss_pct":      daily_pct,
            "weekly_loss_pct":     weekly_pct,
            "cb_tripped":          cb_tripped,
            "positions_flattened": positions_flattened,
            "trailing_evaluated":  trailing_evaluated,
            "trailing_ratcheted":  trailing_ratcheted,
            "trailing_converted":  trailing_converted,
            "duration_seconds":    duration,
            "error_message":       error_message,
        })
    except Exception as exc:
        log.error("Could not persist intraday_run_log row: %s", exc)

    # ── Console summary ──────────────────────────────────────────────────────
    print("=== Summary ===")
    print(f"  Status:               {status}")
    print(f"  Daily Δ:              {daily_pct:+.2%}" if daily_pct is not None else "  Daily Δ:              n/a")
    print(f"  Weekly Δ:             {weekly_pct:+.2%}" if weekly_pct is not None else "  Weekly Δ:             n/a")
    print(f"  CB tripped:           {cb_tripped if cb_tripped is not None else 'n/a'}")
    print(f"  Positions flattened:  {positions_flattened}")
    print(f"  Trailing evaluated:   {trailing_evaluated}")
    print(f"  Trailing ratcheted:   {trailing_ratcheted}")
    print(f"  Trailing converted:   {trailing_converted}")
    print(f"  Duration:             {duration:.1f}s")
    if error_message:
        print(f"  Error:                {error_message}")
    print()

    return 0


def _force_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 with replace-on-error.

    The batch file sets PYTHONUTF8=1 which solves this at process start, but
    ad-hoc invocations from a regular shell on Windows default stdout/stderr
    to cp1252 — at which point the ``✓ ⚠ 🔴 Δ`` characters in our prints
    raise UnicodeEncodeError mid-run, taking the script down before it can
    write its intraday_run_log row.  The "always exit 0" guarantee depends
    on stdout writes not raising.  ``errors='replace'`` degrades gracefully
    on terminals that don't support UTF-8 (chars become '?', no crash).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            # Older Python or already-wrapped streams — leave as-is.  The
            # last-line exception handler in main() avoids unicode in its
            # fallback message so the script still exits 0 cleanly.
            pass


def main() -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(description="Intraday lightweight runner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Log decisions without submitting orders (default: True)",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Enable CB-flatten + trail-conversion paths "
             "(requires paper_orders_enabled=True or LIVE mode)",
    )
    parser.add_argument(
        "--symbol",
        default="",
        metavar="SYM",
        help="(informational) symbol filter — manager evaluates all longs anyway",
    )
    args = parser.parse_args()

    try:
        return run(dry_run=args.dry_run, symbol_filter=args.symbol)
    except SystemExit:
        raise
    except BaseException as exc:
        # Last-line-of-defense: any unhandled exception still results in exit 0
        # (with a status='error' row written if possible).  Task Scheduler
        # retry-storm avoidance trumps loud failure here.
        log.error("Intraday check top-level raised: %s\n%s",
                  exc, traceback.format_exc())
        try:
            log_intraday_run({
                "run_id":              str(uuid.uuid4()),
                "run_timestamp":       _now(),
                "mode":                "intraday",
                "status":              "error",
                "daily_loss_pct":      None,
                "weekly_loss_pct":     None,
                "cb_tripped":          None,
                "positions_flattened": 0,
                "trailing_evaluated":  0,
                "trailing_ratcheted":  0,
                "trailing_converted":  0,
                "duration_seconds":    None,
                "error_message":       f"top-level raised: {exc}",
            })
        except Exception:
            pass
        # Deliberately ASCII-only: this is the path that runs when
        # _force_utf8_streams() itself failed (or wasn't called).  A unicode
        # print here would re-raise and the script would exit non-zero,
        # defeating the Task Scheduler retry-storm avoidance.
        try:
            print(f"[ERROR] Intraday check raised an unhandled exception: {exc}")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
