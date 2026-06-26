"""
Phase B — live-fill reconciliation (reqExecutions → fill_log → trade_log).

GTC bracket legs (STP / LMT / TRAIL) fill *between* daily runs at IBKR, so a
filled exit leaves no CLOSED_LONG / order_decisions / trade_log row — the
position simply disappears from the next run's IBKR positions list.  IBKR
retains ~7 days of execution history server-side via reqExecutions, so a polling
reconciliation is strictly more robust than a live execDetails subscription: it
tolerates Gateway downtime, tolerates skipped runs, and is idempotent on re-run.

This module is the shared core called by BOTH the daily signal_runner Phase 1
hook and the standalone scripts/reconcile_fills.py CLI — no duplicated logic.
The IBKR fetch is dependency-injected as a callable so tests can feed canned
execution dicts without mocking the async reqExecutions dance.

Two passes:
  1. INGEST  — raw executions → fill_log (idempotent on exec_id).
  2. AGGREGATE— paired entry/exit fills → trade_log (source='live'),
                idempotent on the closing exit_exec_id.

COMMISSION-RACE NOTE (do not "fix" upsert_fill into a plain insert-or-ignore):
IBKR's commissionReport can arrive on a *later* fetch than the Execution itself.
``data.database.upsert_fill`` therefore allows exactly one mutation to an existing
row — refreshing commission / realized_pnl when they were previously NULL — and
returns "cost_updated" when it does.  Freezing the row on first insert would
leave commission NULL forever and silently corrupt trade_log.pnl.

NET-P&L CONVENTION (matches source='walk_forward' rows + the trade_log.pnl-is-net
architectural decision):  pnl is stored NET of commissions.
    position_value = entry_px * shares
    pnl_pct        = (exit_px - entry_px) / entry_px - (commissions / position_value)
    pnl            = pnl_pct * entry_px * shares          # == (exit-entry)*shares - commissions
    costs_charged  = commissions (dollars, for display reconstruction only)
Never compute net_pnl = pnl - costs_charged anywhere — that double-counts fees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from core.logger import get_logger
from data.database import (
    get_fills,
    get_latest_approved_bracket,
    get_reconciliation_state,
    has_cb_flatten_near,
    has_closed_long_near,
    has_converted_trailing_before,
    live_trade_exists,
    live_trade_uses_exec_ids,
    log_trade,
    set_reconciliation_state,
    upsert_fill,
)

log = get_logger("execution.reconciliation")

_RETENTION_DAYS = 7   # IBKR server-side reqExecutions horizon


@dataclass
class ReconcileResult:
    n_new_fills: int = 0
    n_cost_updated: int = 0
    n_skipped_fills: int = 0
    n_trades_written: int = 0
    n_trades_skipped: int = 0   # dedup hits (already reconciled)
    n_deferred_cost: int = 0    # round trips withheld pending commissionReport
    n_orphans: int = 0          # symbols not flat at end of window
    n_missed_exits: int = 0     # net>0 orphans that are FLAT at the broker (exit fill missed)
    n_skipped_inverted: int = 0 # pairings rejected for exit_ts <= entry_ts
    window_start: Optional[datetime] = None
    # Symbols for which a source='live' exit row was actually written this run.
    # Callers with data-fetch access (signal_runner Phase 1, scripts/reconcile_flex)
    # refresh these symbols' recent bars immediately so the exit-day OHLCV bar is
    # finalised in the same run that records the trade — closing the ~1-day stale-bar
    # window for rotated-out long-held names (AXTI/GEV 2026-06-09; losers_2026-06.md §5a).
    exited_symbols: set = field(default_factory=set)


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalise an execution timestamp to UTC-naive.

    ``Execution.time`` from ib_insync is tz-aware UTC.  We assert UTC (or coerce
    via astimezone) before stripping tzinfo rather than blindly calling
    ``.replace(tzinfo=None)`` — a non-UTC datetime slipping through would corrupt
    the last_reconciled_ts watermark comparison (which is done against UTC-naive
    values throughout the DB).
    """
    if dt.tzinfo is None:
        return dt   # already naive; assume the caller produced UTC
    utc_offset = dt.utcoffset()
    if utc_offset not in (None, timedelta(0)):
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _vwap(fills: list[dict]) -> float:
    total_sh = sum(f["shares"] for f in fills)
    if total_sh == 0:
        return 0.0
    return sum(f["price"] * f["shares"] for f in fills) / total_sh


def _infer_exit_reason(symbol: str, exit_px: float, exit_ts: datetime,
                       closing_order_type: Optional[str]) -> tuple[str, str]:
    """Return (exit_reason, exit_reason_source) for a live round trip.

    Session-independent: the closing fill's ``order_type`` only resolves for
    current-session orders, so off-session bracket fills (the common case for the
    invisible exits this whole feature exists to capture) fall through to the
    trailing-log and order-decisions-price-match paths before defaulting.
    """
    # 1. order_lookup — order_type resolved in this session.
    if closing_order_type in ("STP", "STP LMT"):
        return "stop", "order_lookup"
    if closing_order_type == "LMT":
        return "tp", "order_lookup"
    if closing_order_type == "TRAIL":
        return "trailing", "order_lookup"

    # 2. cb_flatten — a CB_FLATTENED order_decision near this fill.  A
    #    circuit-breaker liquidation (flatten_all_longs) cancels the bracket
    #    children and submits a plain MKT sell, so it never resolves via step 1
    #    (off-session ⇒ closing_order_type is None) and WOULD otherwise be
    #    mislabeled by the trailing-log step below (a stale CONVERTED row for the
    #    same symbol → "trailing", e.g. C/GLW on the 2026-06-10 17-position CB
    #    flatten) or fall through to "manual_close".  CB_FLATTENED is the
    #    authoritative record (one per symbol per flatten event) so it must win
    #    BEFORE the heuristic trailing/price-match branches.
    if has_cb_flatten_near(symbol, exit_ts):
        return "cb_flatten", "cb_flatten"

    # 3. trailing_log — a CONVERTED trailing row predates this fill.
    if has_converted_trailing_before(symbol, exit_ts):
        return "trailing", "trailing_log"

    # 4. order_decisions_price_match — match exit_px to the recorded bracket,
    #    DIRECTIONALLY and gap-aware (long-only live case).  A long's TP (sell
    #    LMT) fills at-or-ABOVE the TP level — on a gap-up open it fills well
    #    above it (MRVL 2026-06-02: TP $244.89, gap-up fill $255.90).  A long's
    #    stop (sell STP) fills at-or-BELOW the stop level — on a gap-down it
    #    fills well below.  A symmetric tight band (the prior abs(exit-level)<=tol)
    #    missed BOTH gap-through cases — exactly the off-session scenario Phase B
    #    exists to capture — so match by side, not by proximity.  Prices that land
    #    between stop and TP match neither and fall through to the branches below.
    bracket = get_latest_approved_bracket(symbol, exit_ts)
    stop  = bracket.get("stop_price") if bracket else None
    tp    = bracket.get("take_profit_price") if bracket else None
    entry = bracket.get("entry_price") if bracket else None
    if bracket:
        tol = max(0.05, exit_px * 0.001)
        if tp is not None and exit_px >= tp - tol:
            return "tp", "order_decisions_price_match"
        if stop is not None and exit_px <= stop + tol:
            return "stop", "order_decisions_price_match"

    # 5. model-close — a CLOSED_LONG decision near the fill is the authoritative
    #    record of a signal-flip close.  Fires on MKT (in-session) OR an unresolved
    #    order_type: the in-session reqExecutions poll returns order_type=None for
    #    an order that has already filled and gone, so an off-session model close
    #    reconciled by that poll would otherwise miss this branch (STP/LMT/TRAIL
    #    already returned at step 1, so by here order_type is only MKT or None).
    if closing_order_type in ("MKT", None) and has_closed_long_near(symbol, exit_ts):
        return "signal_flip", "default"

    # 6. bracket_residual — process of elimination for a long-only position with a
    #    KNOWN resting bracket.  Having ruled out the TP (step 4 up-match), the
    #    trailing leg (steps 1/3), a CB flatten (step 2) and a model close (step 5),
    #    a loss-side exit (below the recorded entry) can only be the protective STP.
    #    The price-match in step 4 misses it when the stop fills slightly ABOVE its
    #    trigger — IBKR's paper sim / the opening auction can fill a triggered STP a
    #    little above the trigger on a gap day (CVX 2026-06-25: STP $169.60 filled
    #    $170.21 at the open, $0.61 above the trigger), so `exit_px <= stop + tol`
    #    fails and the trade would otherwise mislabel as manual_close.
    if stop is not None and entry is not None and exit_px < entry:
        return "stop", "bracket_residual"

    # 7. default — no bracket on record / an above-entry exit we can't attribute.
    return "manual_close", "default"


def reconcile_fills(
    fetch_executions: Callable[[Optional[datetime]], list[dict]],
    *,
    since: Optional[datetime] = None,
    symbol: Optional[str] = None,
    dry_run: bool = False,
    account: Optional[str] = None,
    source: str = "live",
    live_positions: Optional[Iterable[str]] = None,
) -> ReconcileResult:
    """Reconcile IBKR executions into fill_log + trade_log.

    ``fetch_executions`` is injected: in production it wraps
    ``loop.run_until_complete(ibkr.get_executions(since))``; in tests it returns
    canned execution dicts.  Returns a ReconcileResult summary.

    ``live_positions`` (optional) is the set of symbols currently held at the
    broker.  When supplied, a net>0 orphan (entry with no matching exit) whose
    symbol is NOT in that set is FLAT at the broker — i.e. the position closed
    but its exit fill was never ingested (aged out of reqExecutions before any
    run polled it; the GE/VRT 2026-06-08 silent-drop class).  It is surfaced as
    a distinct "missed exit" WARNING + ``n_missed_exits`` count instead of being
    silently deferred forever as "still open".  When ``None`` (the Flex-backfill
    path, tests, any caller without broker state) the legacy behaviour is
    preserved exactly — every net>0 orphan is treated as still-open.
    """
    result = ReconcileResult()
    held = ({s.upper() for s in live_positions}
            if live_positions is not None else None)

    # ── Resolve the reconciliation window ────────────────────────────────────
    if since is None:
        state = get_reconciliation_state(source, account)
        if state and state.get("last_reconciled_ts"):
            since = state["last_reconciled_ts"]
        else:
            since = _utc_now_naive() - timedelta(days=_RETENTION_DAYS)
    result.window_start = since

    raw = fetch_executions(since) or []
    log.info("Reconciliation: fetched %d execution(s) since %s", len(raw), since)

    # ── Pass 1: ingest raw fills (idempotent on exec_id) ─────────────────────
    max_exec_time: Optional[datetime] = None
    for ex in raw:
        ex = dict(ex)
        if ex.get("exec_time") is not None:
            ex["exec_time"] = _to_naive_utc(ex["exec_time"])
        if symbol and ex.get("symbol") != symbol:
            continue
        # Ingest everything IBKR returns (its ~7-day retention is the real
        # bound — get_executions uses no server-side time filter).  Ingesting
        # the full set keeps fill_log complete so round trips whose entry sits
        # near the window edge still pair; dedup makes it idempotent.  `since`
        # is used only for window_start reporting + watermark seeding.
        et = ex.get("exec_time")
        if et is not None and (max_exec_time is None or et > max_exec_time):
            max_exec_time = et

        if dry_run:
            log.info("  [dry-run] would ingest %s %s %s %.0f@%.2f",
                     ex.get("exec_id"), ex.get("symbol"), ex.get("side"),
                     ex.get("shares") or 0, ex.get("price") or 0.0)
            result.n_new_fills += 1
            continue

        action = upsert_fill(ex)
        if action == "inserted":
            result.n_new_fills += 1
        elif action == "cost_updated":
            result.n_cost_updated += 1
        else:
            result.n_skipped_fills += 1

    if dry_run:
        # Aggregate against whatever is already in fill_log so the operator sees
        # would-be trades.  We don't write trade_log.
        _aggregate(get_fills(symbol=symbol), result, dry_run=True,
                   live_positions=held)
        log.info("Reconciliation [dry-run]: %s", result)
        return result

    # ── Pass 2: aggregate paired fills → trade_log ───────────────────────────
    # Aggregate over ALL fills for the touched symbols (NOT just the fetch
    # window): a round trip can span the watermark boundary (entry on one run,
    # exit on a later run), so window-scoping the aggregation would lose the
    # entry leg.  The live_trade_exists dedup guard makes re-walking completed
    # round trips a cheap no-op.  fill_log is small (a few fills/week), so the
    # all-time walk per touched symbol is negligible.
    _aggregate(get_fills(symbol=symbol), result, dry_run=False,
               live_positions=held)

    # ── Advance the watermark ────────────────────────────────────────────────
    if max_exec_time is not None:
        set_reconciliation_state(
            source=source,
            account=account,
            last_reconciled_ts=max_exec_time,
            last_run_ts=_utc_now_naive(),
            last_n_fills=result.n_new_fills,
            notes=f"trades_written={result.n_trades_written} orphans={result.n_orphans}",
        )

    log.info("Reconciliation: %s", result)
    return result


def _aggregate(fills: list[dict], result: ReconcileResult, *, dry_run: bool,
               live_positions: Optional[set] = None) -> None:
    """Pair entry/exit fills per (symbol, conid) and write round-trip trades.

    Walks fills chronologically accumulating net position; a round trip closes
    when net returns to flat.  Long-only: entries are BUY, exits are SELL.
    Symbols never returning to flat are orphans (entry with no matching exit,
    e.g. a still-open position) — left in fill_log for a later run.

    ``live_positions`` (when not None) is the set of symbols held at the broker;
    a net>0 orphan whose symbol is absent from it is flat at the broker — its
    exit fill was missed — and is surfaced as a "missed exit" rather than as a
    still-open position.  See ``reconcile_fills`` for the rationale.
    """
    # Group by (symbol, conid).
    groups: dict[tuple, list[dict]] = {}
    for f in fills:
        groups.setdefault((f["symbol"], f.get("conid")), []).append(f)

    for (sym, _conid), group in groups.items():
        group.sort(key=lambda f: f["exec_time"])
        net = 0.0
        entry_fills: list[dict] = []
        exit_fills: list[dict] = []

        for f in group:
            signed = f["shares"] if f["side"] == "BUY" else -f["shares"]
            if f["side"] == "BUY":
                entry_fills.append(f)
            else:
                exit_fills.append(f)
            net += signed

            if entry_fills and exit_fills and abs(net) < 1e-9:
                # Round trip complete.
                _write_round_trip(sym, entry_fills, exit_fills, result, dry_run=dry_run)
                net = 0.0
                entry_fills = []
                exit_fills = []

        if (entry_fills or exit_fills) and abs(net) > 1e-9:
            # Leftover fills that never closed into a flat round trip.
            result.n_orphans += 1
            if net > 0:
                if live_positions is not None and sym.upper() not in live_positions:
                    # FLAT at the broker but net>0 in fill_log → the position
                    # closed and its exit fill was never ingested (aged out of
                    # reqExecutions before any run polled it).  This is the
                    # GE/VRT 2026-06-08 silent-drop class — the inverse of the
                    # net<0 lone-exit below.  The legacy "leaving fills for a
                    # later run" message is a lie here: the exit will NEVER
                    # arrive via reqExecutions, so a realised round trip stays
                    # unrecorded indefinitely.  Surface it loudly so it's
                    # recoverable (Flex backfill).  Deliberately NOT synthesising
                    # the exit — same "trade_log = end-to-end-observed trades
                    # only" discipline as the net<0 branch.
                    result.n_missed_exits += 1
                    log.warning("Reconciliation: %s entry fill(s) with net=%.2f "
                                "but FLAT at the broker — exit fill was missed "
                                "(aged out of reqExecutions); realised exit is "
                                "unrecorded.  Recover via a Flex backfill",
                                sym, net)
                else:
                    # Open long / partial — entry with no (full) matching exit
                    # yet, and the broker still shows the position held (or no
                    # broker state was supplied to disprove it).
                    log.warning("Reconciliation: %s not flat at window end "
                                "(net=%.2f) — open position / partial, leaving "
                                "fills for a later run", sym, net)
            else:
                # net < 0: exit fill(s) with NO matching entry in fill_log — the
                # 2026-06-05 GLW silent-drop class.  A between-run exit whose
                # entry leg was never ingested (missed / aged out of the
                # reqExecutions window) previously fell through BOTH the
                # round-trip write AND the old orphan branch (which only fired
                # for entry_fills), so a real realised exit vanished with no row
                # and no warning.  Surface it so it is visible and recoverable
                # (re-run reconcile_fills with a wider --since to ingest the
                # entry, or Flex-backfill).  Deliberately NOT synthesising an
                # entry from order_decisions — that would violate the
                # "trade_log = end-to-end-observed trades only" decision
                # (CHANGELOG 2026-05-29) and risks a new corrupt-row class.
                log.warning("Reconciliation: %s exit fill(s) with no matching "
                            "entry in fill_log (net=%.2f) — between-run exit "
                            "whose entry was not ingested; realised exit is "
                            "unrecorded.  Recover via a wider reconcile_fills "
                            "--since or a Flex backfill", sym, net)


def _write_round_trip(symbol: str, entry_fills: list[dict], exit_fills: list[dict],
                      result: ReconcileResult, *, dry_run: bool) -> None:
    entry_px = _vwap(entry_fills)
    exit_px  = _vwap(exit_fills)
    shares   = sum(f["shares"] for f in entry_fills)
    entry_ts = min(f["exec_time"] for f in entry_fills)
    closing  = max(exit_fills, key=lambda f: f["exec_time"])
    exit_ts  = closing["exec_time"]
    exit_exec_id  = closing["exec_id"]
    entry_exec_id = min(entry_fills, key=lambda f: f["exec_time"])["exec_id"]

    # Dedup (real-mode only — dry_run still reports would-write lines for
    # already-reconciled trips).  Skip silently if any constituent fill is
    # already a leg of a live trade.  live_trade_exists covers the normal
    # exit-leg idempotency; live_trade_uses_exec_ids additionally closes the
    # both-legs re-pairing gap that let the 2026-06-05 SLV duplicate through
    # (the orphan-short fills were already consumed by id=2002 but with a
    # different exit leg, so the exit-only guard didn't catch the reverse pair).
    if not dry_run and (
        live_trade_exists(exit_exec_id)
        or live_trade_uses_exec_ids([entry_exec_id, exit_exec_id])
    ):
        result.n_trades_skipped += 1
        return

    # Guard: a valid long round trip must have entry strictly before exit.  A
    # SELL-then-BUY sequence (an orphaned short — e.g. the 2026-04-29 SLV
    # orphan-stop episode) collects all BUYs into entry_fills and all SELLs into
    # exit_fills regardless of chronological order, then trips the net==flat
    # check with exit_ts <= entry_ts.  Persisting it creates a
    # chronologically-impossible row that double-counts the loss against the
    # correctly-labelled short (id=2002 vs the regenerated id=2022 on
    # 2026-06-05).  Reject + WARN rather than corrupt trade_log.
    if exit_ts <= entry_ts:
        result.n_skipped_inverted += 1
        log.warning("[%s] skipping inverted round trip — exit_ts %s <= entry_ts "
                    "%s (orphaned-short fills paired as a long; entry_exec=%s "
                    "exit_exec=%s)", symbol, exit_ts, entry_ts,
                    entry_exec_id, exit_exec_id)
        return

    # Defer the round trip if any constituent fill's commissionReport hasn't
    # posted yet (commission IS NULL) — writing now would bake commission=0 into
    # trade_log.pnl, and the per-round-trip dedup guard would then block the
    # correction once the cost lands.  Leaving it unwritten means the next run
    # (after upsert_fill cost-updates the fills) writes it with the true net P&L.
    # NOTE: 0.0 is a real value (paper-account zero commission) — only None defers.
    all_fills = entry_fills + exit_fills
    if any(f.get("commission") is None for f in all_fills):
        result.n_deferred_cost += 1
        log.debug("Reconciliation: deferring %s round trip — commission pending "
                  "on %d fill(s)", symbol,
                  sum(1 for f in all_fills if f.get("commission") is None))
        return

    commissions = sum((f.get("commission") or 0.0) for f in all_fills)
    position_value = entry_px * shares

    # Net-P&L convention — see module docstring.
    if position_value > 0:
        pnl_pct = (exit_px - entry_px) / entry_px - (commissions / position_value)
    else:
        pnl_pct = 0.0
    pnl = pnl_pct * entry_px * shares

    exit_reason, exit_reason_source = _infer_exit_reason(
        symbol, exit_px, exit_ts, closing.get("order_type")
    )

    if dry_run:
        log.info("  [dry-run] would write trade %s entry=%.2f exit=%.2f sh=%.0f "
                 "pnl=%.2f reason=%s(%s)", symbol, entry_px, exit_px, shares,
                 pnl, exit_reason, exit_reason_source)
        result.n_trades_written += 1
        return

    log.info("[%s] live trade: entry %.2f → exit %.2f (%.0f sh) pnl=%.2f "
             "reason=%s source=%s", symbol, entry_px, exit_px, shares, pnl,
             exit_reason, exit_reason_source)

    log_trade({
        "source":          "live",
        "run_id":          None,
        "fold_index":      None,
        "symbol":          symbol,
        "signal":          "BUY",
        "entry_ts":        entry_ts,
        "entry_px":        entry_px,
        "exit_ts":         exit_ts,
        "exit_px":         exit_px,
        "exit_reason":     exit_reason,
        "shares":          shares,
        "pnl":             pnl,
        "pnl_pct":         pnl_pct,
        "costs_charged":   commissions,
        "entry_exec_id":   entry_exec_id,
        "exit_exec_id":    exit_exec_id,
        "parent_order_id": closing.get("parent_order_id"),
        "account":         closing.get("account"),
        "recorded_at":     _utc_now_naive(),
    })
    result.n_trades_written += 1
    result.exited_symbols.add(symbol)
