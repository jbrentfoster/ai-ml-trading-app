"""
Daily signal runner — five-phase automation script.

Phases:
  1. Startup      — (Phase B, not dry-run) reconcile off-cycle IBKR fills into
                    fill_log + trade_log via execution/reconciliation.py BEFORE
                    anything else, so exits filled between runs are captured ahead
                    of Phase-4 realised-Kelly sizing; then load config, determine
                    symbol list, snapshot equity from IBKR + auto-trigger circuit
                    breaker if daily/weekly loss thresholds are breached vs. prior
                    snapshots
  2. Data refresh — fetch OHLCV + indicators for each symbol
  3. Signal gen   — drop symbols whose newest bar is older than
                    config.risk.max_bar_staleness_days, then run ensemble
                    predict for each symbol with a saved model
  3.5 Trailing   — TrailingStopManager: bracket TP → standalone TRAIL once a
                   long has moved +activation_atr × ATR above entry (skipped
                   in dry-run; opt-in via config.risk.trailing_stop_enabled)
  3.6 Hold-timeout — flatten held longs whose most recent passed-gate BUY
                     in signal_log is older than config.risk.max_hold_days
                     (skipped in dry-run; opt-in via
                     config.risk.hold_timeout_enabled)
  4. Risk / order — PortfolioGuard + OrderManager per actionable signal
  5. Summary      — print stats, write signal_runner_log row

Usage:
    python signal_runner.py                     # dry-run all symbols (default)
    python signal_runner.py --dry-run           # explicit dry-run (same as default)
    python signal_runner.py --no-dry-run        # submit live paper orders — requires
                                                #   IB Gateway running AND
                                                #   trading.paper_orders_enabled=True in config
    python signal_runner.py --symbol AAPL       # single symbol
    python signal_runner.py --schedule          # run forever at 09:35 daily
                                                #   (manual use only — production uses run_daily.bat)

Live-order submission notes (--no-dry-run):
  * Each BUY signal is placed as a bracket order (entry LMT + TP LMT + stop STP),
    linked so cancelling one leg cancels the others.
  * All legs are submitted GTC so they survive if the runner fires outside RTH.
  * Prices are rounded to $0.01 tick size (IBKR rejects sub-tick prices with error 110).
  * Use `python open_orders.py` to inspect parked orders, or
    `python open_orders.py --cancel --id ...` to clear stale brackets.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config, TradingMode
from core.logger import get_logger
from data.database import (
    get_equity_snapshot_on_or_before,
    get_latest_buy_signal_ts,
    log_equity_snapshot,
    log_order_decision,
    log_signal,
    log_signal_runner_run,
)
from risk.circuit_breaker import CircuitBreaker
from risk.order_manager import OrderDecision, OrderManager
from risk.trailing_stop import TrailingStopAction, TrailingStopManager

log = get_logger("signal_runner")

# Symbols that represent the same underlying company.
# When one member of a pair has already been decided, the other is skipped.
EQUIVALENT_PAIRS: dict[str, str] = {
    "GOOG":  "GOOGL",
    "GOOGL": "GOOG",
}


# ── Model loader ──────────────────────────────────────────────────────────────

def _load_ensemble(symbol: str):
    """
    Load a saved EnsembleModel for `symbol`.
    Returns the ensemble or None if checkpoints are missing.
    """
    cache = Path("models/cache") / symbol
    lstm_path = cache / "lstm.pt"
    xgb_path  = cache / "xgb.ubj"

    if not (lstm_path.exists() and xgb_path.exists()):
        return None

    try:
        from models.ensemble import EnsembleModel
        ensemble = EnsembleModel(symbol=symbol)
        ensemble.load(str(cache))
        return ensemble
    except Exception as exc:
        log.warning("Could not load model for %s: %s", symbol, exc)
        return None


# ── Phase implementations ─────────────────────────────────────────────────────

def _check_loss_limits_against_baseline(cb: CircuitBreaker) -> tuple[bool, str]:
    """
    Pull NLV from IBKR, compare against prior snapshots, write today's
    snapshot, and call ``cb.check_loss_limits()``.

    Returns (halted, reason).  Degrades gracefully:
      * IBKR unreachable                     → no-op (returns False, "")
      * No prior baseline snapshot           → write today's, skip CB check
      * Snapshot stored — daily/weekly pcts computed and CB checked

    Daily baseline = most recent snapshot strictly before today.
    Weekly baseline = most recent snapshot on or before (today - 7 days).
    """
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    today_str = today.strftime("%Y-%m-%d")

    # Open a short-lived IBKR connection just for this snapshot pull.
    # _connect_ibkr_if_needed() handles event-loop binding; we pass dry_run=False
    # because we *do* need IBKR here, regardless of whether orders fire later.
    # Reuse the same gating logic — only attempt when paper/live is active.
    needs_ibkr = (
        config.trading.mode == TradingMode.LIVE
        or (
            config.trading.mode == TradingMode.SIMULATION
            and config.trading.paper_orders_enabled
        )
    )
    if not needs_ibkr:
        print("  ⓘ  CB auto-check skipped (paper_orders_enabled=False).")
        return False, ""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ibkr = None
    try:
        from execution.ibkr_connection import IBKRConnection
        ibkr = IBKRConnection()
        connected = loop.run_until_complete(ibkr.connect())
        if not connected:
            print("  ⚠  IBKR unreachable — CB auto-check skipped.")
            log.warning("CB auto-check skipped: IBKR connect failed.")
            return False, ""

        summary = loop.run_until_complete(ibkr.get_account_summary())
        nlv = float(summary.net_liquidation or 0.0)
        if nlv <= 0:
            print("  ⚠  IBKR returned NLV <= 0 — CB auto-check skipped.")
            return False, ""

        # Look up baselines BEFORE writing today's snapshot, so re-runs on the
        # same day still compare against yesterday's value rather than today's.
        daily_base = get_equity_snapshot_on_or_before(
            (today - timedelta(days=1)).strftime("%Y-%m-%d")
        )
        weekly_base = get_equity_snapshot_on_or_before(
            (today - timedelta(days=7)).strftime("%Y-%m-%d")
        )

        log_equity_snapshot({
            "snapshot_date":   today_str,
            "net_liquidation": nlv,
            "total_cash":      float(summary.total_cash or 0.0),
            "unrealized_pnl":  float(summary.unrealized_pnl or 0.0),
            "realized_pnl":    float(summary.realized_pnl or 0.0),
            "recorded_at":     datetime.now(timezone.utc).replace(tzinfo=None),
        })

        if daily_base is None and weekly_base is None:
            print(f"  ⓘ  No prior equity snapshot — baseline seeded (NLV ${nlv:,.2f}).")
            return False, ""

        daily_loss_pct = 0.0
        weekly_loss_pct = 0.0
        if daily_base and daily_base["net_liquidation"]:
            daily_loss_pct = (nlv - daily_base["net_liquidation"]) / daily_base["net_liquidation"]
        if weekly_base and weekly_base["net_liquidation"]:
            weekly_loss_pct = (nlv - weekly_base["net_liquidation"]) / weekly_base["net_liquidation"]

        print(
            f"  Equity: NLV ${nlv:,.2f}  |  "
            f"Daily Δ {daily_loss_pct:+.2%}  |  Weekly Δ {weekly_loss_pct:+.2%}"
        )

        triggered = cb.check_loss_limits(daily_loss_pct, weekly_loss_pct)
        if triggered:
            halted, reason = cb.is_halted()
            return halted, reason
        return False, ""
    except Exception as exc:
        print(f"  ⚠  CB auto-check failed: {exc}")
        log.warning("CB auto-check failed: %s", exc, exc_info=True)
        return False, ""
    finally:
        if ibkr is not None:
            try:
                loop.run_until_complete(ibkr.disconnect())
            except Exception:
                pass
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def _fetch_held_long_symbols() -> set[str]:
    """
    Pull the set of symbols with an open long position from IBKR.

    Used by Phase 1 to ensure that any held long is included in the operational
    symbol list — even if Stage-3 universe rescore has dropped it.  Without
    this, an orphan held position stops getting OHLCV refreshes (Phase 2),
    receives no signal evaluation / SELL exit path (Phase 3), and is evaluated
    by the trailing-stop manager against a stale cached close (Phase 3.5).

    Mirrors the connect/use/disconnect pattern in
    ``_check_loss_limits_against_baseline``.  Returns an empty set when:
      * mode is dry-run / SIMULATION without paper_orders_enabled (no IBKR)
      * IBKR connect fails
      * the positions call raises
    """
    needs_ibkr = (
        config.trading.mode == TradingMode.LIVE
        or (
            config.trading.mode == TradingMode.SIMULATION
            and config.trading.paper_orders_enabled
        )
    )
    if not needs_ibkr:
        return set()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ibkr = None
    try:
        from execution.ibkr_connection import IBKRConnection
        ibkr = IBKRConnection()
        connected = loop.run_until_complete(ibkr.connect())
        if not connected:
            log.warning("Held-position fetch skipped: IBKR connect failed.")
            return set()

        raw = loop.run_until_complete(ibkr.get_positions())
        held: set[str] = set()
        for p in raw:
            shares = int(p.get("quantity", 0) or 0)
            sym    = p.get("symbol")
            if shares > 0 and sym:
                held.add(sym)
        return held
    except Exception as exc:
        log.warning("Could not fetch held positions: %s", exc, exc_info=True)
        return set()
    finally:
        if ibkr is not None:
            try:
                loop.run_until_complete(ibkr.disconnect())
            except Exception:
                pass
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def _phase1_reconcile_fills(dry_run: bool) -> None:
    """Phase B: reconcile IBKR fills into fill_log + trade_log.

    Runs at the very start of Phase 1 — *before* the circuit-breaker /
    equity-baseline logic and well before Phase 4 sizing — so any off-cycle
    fills since the last run populate trade_log before realised-Kelly reads it.
    Skipped in dry-run.  Gateway-down → log + continue (no state mutation),
    the same graceful-degradation contract as every other IBKR phase.  Opens
    and closes its own short-lived connection (< 1 s), like Phase 3.5.
    """
    if dry_run:
        return

    ibkr, loop = _connect_ibkr_if_needed(dry_run)
    if ibkr is None or loop is None:
        print("  ⚠  IBKR unreachable — skipping fill reconciliation.")
        return

    try:
        from execution.reconciliation import reconcile_fills
        # Pass the broker's currently-held symbols so reconciliation can tell a
        # genuinely-open net>0 orphan apart from one that's flat at the broker
        # (exit fill missed / aged out — the GE/VRT 2026-06-08 silent-drop class).
        held_symbols = set(_fetch_positions(ibkr, loop).keys())
        result = reconcile_fills(
            lambda since: loop.run_until_complete(ibkr.get_executions(since)),
            live_positions=held_symbols,
        )
        msg = (
            f"  Reconciliation: {result.n_new_fills} new fill(s), "
            f"{result.n_cost_updated} cost-updated, "
            f"{result.n_trades_written} live trade(s) written, "
            f"{result.n_orphans} orphan(s)."
        )
        if result.n_missed_exits:
            msg += (f"  ⚠ {result.n_missed_exits} missed exit(s) "
                    f"(flat at broker, exit fill not ingested — Flex-recover).")
        print(msg)
        # Finalise the exit-day bar(s) for any symbol that just got a live exit
        # row, so a rotated-out long-held name's exit-day OHLCV isn't left as a
        # stale mid-morning partial until the next EOD run (losers_2026-06.md §5a).
        if result.exited_symbols:
            from scripts.refresh_recent_bars import refresh_symbols
            n_bars, _n_ind, _failed = refresh_symbols(result.exited_symbols)
            print(f"  ⟳  Finalised exit-day bars for {len(result.exited_symbols)} "
                  f"reconciled symbol(s): {sorted(result.exited_symbols)} "
                  f"({n_bars} bar(s) overwritten)")
    except Exception as exc:
        log.warning("Fill reconciliation failed: %s", exc)
        print(f"  ⚠  Fill reconciliation failed — {exc}")
    finally:
        try:
            loop.run_until_complete(ibkr.disconnect())
        except Exception as exc:
            log.warning("IBKR disconnect error after reconciliation: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def _phase1_startup(dry_run: bool, symbol_filter: str) -> tuple[list[str], bool, str]:
    """
    Return (symbols, is_halted, halt_reason).
    Logs circuit breaker status and selects the symbol list.

    When not in dry-run, also pulls NLV from IBKR and runs the CB auto-trigger
    against persisted equity snapshots — the breaker fires automatically when
    realized + unrealized losses breach configured thresholds.
    """
    print("=== Phase 1: Startup ===")

    # Phase B — reconcile any off-cycle fills before CB / baseline / sizing.
    _phase1_reconcile_fills(dry_run)

    cb = CircuitBreaker()
    halted, reason = cb.is_halted()
    if halted:
        print(f"  ⚠  Circuit breaker ACTIVE: {reason}")
        print("     No signals will be processed.  Reset via Page 8 or CircuitBreaker.reset().")
    else:
        print("  ✓  Circuit breaker: clear")
        if not dry_run:
            halted, reason = _check_loss_limits_against_baseline(cb)
            if halted:
                print(f"  🔴 Circuit breaker AUTO-TRIGGERED: {reason}")

    mode = config.trading.mode.value
    paper_enabled = config.trading.paper_orders_enabled
    if dry_run:
        mode_label = "DRY_RUN (--dry-run flag)"
    elif config.trading.mode == TradingMode.SIMULATION and not paper_enabled:
        mode_label = "DRY_RUN (SIMULATION + paper_orders_enabled=False)"
    elif config.trading.mode == TradingMode.SIMULATION:
        mode_label = "PAPER (SIMULATION + paper_orders_enabled=True)"
    else:
        mode_label = "LIVE"
    print(f"  Mode: {mode_label}")

    # Symbol list — universe (or static watchlist) plus any held longs that
    # have been dropped by the latest universe rescore.  Without the union,
    # an orphan held position is invisible to Phases 2/3 and trailing-stop
    # activation runs against a stale cached close.
    if symbol_filter:
        symbols = [symbol_filter.upper()]
    elif config.universe.enabled:
        try:
            from data.database import get_universe_assets
            df = get_universe_assets(active_only=True)
            symbols = df["symbol"].tolist() if not df.empty else list(config.data.watchlist)
        except Exception:
            symbols = list(config.data.watchlist)
    else:
        symbols = list(config.data.watchlist)

    # Held-position override.  Single-symbol runs are exempt — the user has
    # explicitly asked for one symbol and we should honour that.
    held_extras: list[str] = []
    if not symbol_filter and not dry_run:
        held = _fetch_held_long_symbols()
        existing = set(symbols)
        held_extras = sorted(s for s in held if s not in existing)
        if held_extras:
            symbols = symbols + held_extras

    if held_extras:
        print(
            f"  Symbols ({len(symbols)}): {len(symbols) - len(held_extras)} from "
            f"universe + {len(held_extras)} held-only ({held_extras})"
        )
    else:
        print(f"  Symbols ({len(symbols)}): {symbols[:10]}{'...' if len(symbols) > 10 else ''}")
    print()
    return symbols, halted, reason


def _phase2_refresh(symbols: list[str]) -> None:
    """Fetch latest OHLCV and indicators for each symbol."""
    print("=== Phase 2: Data refresh ===")
    from data.fetcher import DataFetcher
    from data.indicators import IndicatorEngine

    fetcher = DataFetcher()
    engine  = IndicatorEngine()

    for sym in symbols:
        try:
            df = fetcher.fetch_symbol(sym, interval="1d", days_back=5)
            if not df.empty:
                engine.run(sym, interval="1d")
                print(f"  {sym}: refreshed ({len(df)} bars)")
            else:
                print(f"  {sym}: no data returned")
        except Exception as exc:
            print(f"  {sym}: refresh failed — {exc}")

    print()


def _phase3_signals(symbols: list[str]) -> tuple[list[tuple], int]:
    """
    Run ensemble prediction for each symbol that has a saved model.

    Returns (actionable, skipped_stale):
      * actionable     — list of (signal_result, atr_value) tuples for symbols
                         that passed the gate
      * skipped_stale  — count of symbols dropped because their newest cached
                         daily bar was older than ``config.risk.max_bar_staleness_days``

    The stale-bar gate runs first.  Without it, signals fire against whatever
    bar happens to be latest in SQLite — including week-old data after a
    pipeline outage.
    """
    print("=== Phase 3: Signal generation ===")
    from data.database import get_latest_indicators
    from data.indicators import IndicatorEngine
    from models.signal_gate import SignalGate

    gate      = SignalGate()
    engine    = IndicatorEngine()
    actionable: list[tuple] = []
    skipped_stale = 0
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    max_age = config.risk.max_bar_staleness_days

    for sym in symbols:
        ensemble = _load_ensemble(sym)
        if ensemble is None:
            print(f"  {sym}: no saved model — skipping")
            continue

        df = engine.run(sym, interval="1d")
        if df is None or df.empty:
            print(f"  {sym}: no bars in DB — skipping")
            continue

        # Stale-bar gate.  df.index is a DatetimeIndex (ascending), tz-naive.
        latest_ts = df.index[-1]
        latest_date = (
            latest_ts.date() if hasattr(latest_ts, "date") else latest_ts
        )
        age_days = (today - latest_date).days
        if age_days > max_age:
            skipped_stale += 1
            print(
                f"  {sym}: STALE — newest bar {latest_date} is {age_days}d old "
                f"(> {max_age}d limit)"
            )
            log.warning(
                "Skipping %s: newest bar %s is %dd old (> %dd limit)",
                sym, latest_date, age_days, max_age,
            )
            continue

        try:
            scores = ensemble.predict(df)
            result = gate.evaluate(sym, df, scores)

            # Persist every result (HOLD / BUY / SELL — passed or failed gate)
            # so Page 3's score-history view reflects what the daily runner
            # actually produced.  log_signal swallows its own errors.
            log_signal({
                "symbol":         result.symbol,
                "generated_at":   result.generated_at,
                "bar_timestamp":  result.bar_timestamp,
                "lstm_score":     result.lstm_score,
                "xgb_score":      result.xgb_score,
                "finbert_score":  result.finbert_score,
                "ensemble_score": result.ensemble_score,
                "regime":         result.regime.value,
                "signal":         result.signal,
                "passed_gate":    result.passed_gate,
                "gate_reason":    result.gate_reason,
            })

            ind = get_latest_indicators(sym, "1d")
            atr = ind["atr_14"] if ind else None

            status = f"{result.signal} (score={result.ensemble_score:.3f})"
            if result.passed_gate:
                actionable.append((result, atr))
                print(f"  {sym}: ✓ {status}")
            else:
                print(f"  {sym}: HOLD — {result.gate_reason}")
        except Exception as exc:
            print(f"  {sym}: signal error — {exc}")
            log.warning("Signal generation failed for %s: %s", sym, exc, exc_info=True)

    print()
    return actionable, skipped_stale


def _connect_ibkr_if_needed(dry_run: bool):
    """
    Open an IBKRConnection when the runner intends to actually submit orders.

    Returns (ibkr, loop) — both `None` when a connection isn't required, or
    when an optional connect failed (caller should then fall back to dry-run).

    The same event loop is returned to the caller so subsequent async calls
    (positions, bracket orders) run on the loop the IB client was bound to.

    ib_insync internals call asyncio.get_event_loop() during IB() construction
    and in the Client.connect() path — so we must set our fresh loop as the
    *current* thread loop before instantiating IBKRConnection.  Without that,
    connectAsync fails with "'NoneType' object has no attribute 'connect'"
    because the underlying client grabs `None` from get_event_loop().
    """
    needs_ibkr = (
        not dry_run
        and (
            config.trading.mode == TradingMode.LIVE
            or (
                config.trading.mode == TradingMode.SIMULATION
                and config.trading.paper_orders_enabled
            )
        )
    )
    if not needs_ibkr:
        return None, None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        from execution.ibkr_connection import IBKRConnection
        ibkr = IBKRConnection()
        connected = loop.run_until_complete(ibkr.connect())
        if not connected:
            loop.close()
            asyncio.set_event_loop(None)
            return None, None
        return ibkr, loop
    except Exception as exc:
        log.error("IBKR connect error: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)
        return None, None


def _fetch_positions(ibkr, loop) -> dict:
    """Pull current IBKR positions into the dict shape PortfolioGuard expects."""
    if ibkr is None or loop is None:
        return {}
    try:
        raw = loop.run_until_complete(ibkr.get_positions())
    except Exception as exc:
        log.warning("Could not fetch IBKR positions: %s", exc)
        return {}
    positions: dict = {}
    for p in raw:
        shares = int(p.get("quantity", 0) or 0)
        if shares == 0:
            continue
        positions[p["symbol"]] = {
            "shares":       shares,
            "entry_price":  float(p.get("avg_cost", 0.0) or 0.0),
            "current_price": float(p.get("avg_cost", 0.0) or 0.0),
        }
    return positions


def _fetch_pending_entry_symbols(ibkr, loop, held) -> set[str]:
    """Symbols with a working (unfilled) open order at IBKR that are NOT
    currently held — i.e. an entry bracket whose parent has not filled yet.

    These are invisible to PortfolioGuard's duplicate check, which only sees
    *filled* positions (``_fetch_positions``).  Without this guard a fresh
    same-symbol BUY in Phase 4 stacks a SECOND bracket on top of the still-
    working entry.  Bracket entries are GTC, so an unfilled BUY LMT routinely
    sits for days (e.g. a Friday entry the symbol gapped past at the open),
    which makes the duplicate-stack a real recurring hazard, not a corner case.

    Held symbols are excluded on purpose: their open orders are protective
    TP/STP/TRAIL legs, already handled by the duplicate guard (new BUY) and the
    long-only close path (SELL).  Returns an empty set when IBKR is unavailable
    (dry-run, paper disabled, or a fetch error) — this is a best-effort guard,
    never a hard blocker on order submission.
    """
    if ibkr is None or loop is None:
        return set()
    try:
        orders = loop.run_until_complete(ibkr.get_open_orders())
    except Exception as exc:
        log.warning("Could not fetch IBKR open orders for dedup: %s", exc)
        return set()
    held_syms = set(held.keys()) if isinstance(held, dict) else set(held)
    pending: set[str] = set()
    for o in orders:
        sym = o.get("symbol")
        if not sym or sym in held_syms:
            continue
        remaining = o.get("remaining")
        # openTrades() only returns active orders; treat unknown remaining as
        # working.  A partially-filled entry makes the symbol held (non-zero
        # shares) → excluded above and caught by the duplicate guard instead.
        if remaining is None or remaining > 0:
            pending.add(sym)
    return pending


def _phase3_5_trailing_stops(dry_run: bool, run_id: str = "") -> int:
    """
    Phase 3.5: Walk existing long positions and convert qualifying bracket
    take-profits into standalone trailing stops.

    Returns the count of positions converted in this run (used for the Phase 5
    summary).  Skipped entirely when:
      * `dry_run=True` (no live order mutations in dry-run)
      * paper_orders_enabled=False in SIMULATION mode (same reason)
      * config.risk.trailing_stop_enabled=False (opt-in feature)

    Opens and closes its own IBKR connection — small redundancy with Phase 4,
    but keeps the phase boundary clean.  Each connect/disconnect is < 1 s.
    """
    if dry_run or not config.risk.trailing_stop_enabled:
        return 0
    if (
        config.trading.mode == TradingMode.SIMULATION
        and not config.trading.paper_orders_enabled
    ):
        return 0

    print("=== Phase 3.5: Trailing stop management ===")

    ibkr, loop = _connect_ibkr_if_needed(dry_run)
    if ibkr is None or loop is None:
        print("  ⚠  IBKR unreachable — skipping trailing stop management.")
        print()
        return 0

    converted = 0
    try:
        manager = TrailingStopManager(ibkr_connection=ibkr, event_loop=loop)
        actions = manager.manage(run_id=run_id)
        if not actions:
            print("  No long positions found.")
        for act in actions:
            if act.action == "CONVERTED":
                converted += 1
                print(
                    f"  {act.symbol}: CONVERTED — entry={act.entry_price:.2f} "
                    f"current={act.current_price:.2f} trail=${act.trail_amount:.2f}"
                )
            elif act.action == "FAILED":
                print(f"  {act.symbol}: FAILED — {act.reason}")
            else:
                print(f"  {act.symbol}: skipped ({act.reason})")
        print()
        return converted
    finally:
        try:
            loop.run_until_complete(ibkr.disconnect())
        except Exception as exc:
            log.warning("IBKR disconnect error after trailing-stop phase: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def _phase3_6_hold_timeouts(dry_run: bool, run_id: str = "") -> int:
    """
    Phase 3.6: flatten any held long whose most recent passed-gate BUY signal
    is older than ``config.risk.max_hold_days``.

    Guards against positions sitting indefinitely in sparse-signal regimes —
    once a BUY fills, neither stop nor TP nor a fresh SELL may ever fire, so
    without this gate a stale winner / loser can hold for months.  The
    "re-confirming signal" semantic is preferred over a pure time-based stop:
    if the model still says BUY today (or any day within the window), the
    position is *not* stale and the timeout does not fire.

    Returns the count of positions timed out in this run (used for Phase 5
    summary).  Skipped entirely when:
      * ``dry_run=True`` (no live order mutations in dry-run)
      * ``paper_orders_enabled=False`` in SIMULATION mode
      * ``config.risk.hold_timeout_enabled=False`` (opt-in feature, default off)
      * ``config.risk.max_hold_days <= 0`` (defensive — would otherwise flatten
        every held long on the next run)

    Persists each closure to ``order_decisions`` with decision='CLOSED_TIMEOUT'
    so Page 8 can surface a retrospective view alongside CLOSED_LONG / APPROVED.
    Symbols with NO BUY signal in ``signal_log`` are skipped (we have no anchor
    for staleness — could be a manual position or a holding from before the
    runner started writing signal_log rows).
    """
    if dry_run or not config.risk.hold_timeout_enabled:
        return 0
    if config.risk.max_hold_days <= 0:
        return 0
    if (
        config.trading.mode == TradingMode.SIMULATION
        and not config.trading.paper_orders_enabled
    ):
        return 0

    print("=== Phase 3.6: Hold timeout ===")

    ibkr, loop = _connect_ibkr_if_needed(dry_run)
    if ibkr is None or loop is None:
        print("  ⚠  IBKR unreachable — skipping hold-timeout phase.")
        print()
        return 0

    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    max_days = config.risk.max_hold_days
    timed_out = 0

    try:
        positions   = loop.run_until_complete(ibkr.get_positions())
        open_orders = loop.run_until_complete(ibkr.get_open_orders())

        long_positions = [p for p in positions if int(p.get("quantity", 0) or 0) > 0]
        if not long_positions:
            print("  No long positions held.")
            print()
            return 0

        for pos in long_positions:
            symbol = pos.get("symbol")
            shares = int(pos.get("quantity", 0) or 0)
            if not symbol or shares <= 0:
                continue

            latest_buy = get_latest_buy_signal_ts(symbol)
            if latest_buy is None:
                print(f"  {symbol}: skipped — no BUY signal history (manual position?)")
                continue

            # signal_log.bar_timestamp is a tz-naive datetime
            buy_date = latest_buy.date() if hasattr(latest_buy, "date") else latest_buy
            age_days = (today - buy_date).days

            if age_days <= max_days:
                print(
                    f"  {symbol}: ok — last BUY {buy_date} ({age_days}d ago, "
                    f"limit {max_days}d)"
                )
                continue

            # Stale — flatten.  Mirror OrderManager._close_long_position's
            # cancel-children-then-market-close ordering: a stale bracket left
            # live after the position goes to 0 can fire against no shares and
            # open an unintended short, bypassing allow_short_selling=False.
            print(
                f"  {symbol}: TIMEOUT — last BUY {buy_date} ({age_days}d ago "
                f"> {max_days}d limit), closing {shares} shares"
            )
            entry_price = float(pos.get("avg_cost", 0.0) or 0.0)
            closed_ok = _close_for_timeout(
                ibkr=ibkr,
                loop=loop,
                symbol=symbol,
                shares=shares,
                open_orders=open_orders,
            )
            decision_label = "CLOSED_TIMEOUT" if closed_ok else "REJECTED"
            reject_reason = (
                ""
                if closed_ok
                else f"Hold-timeout close failed (last BUY {buy_date}, {age_days}d ago)"
            )
            try:
                log_order_decision({
                    "run_id":            run_id,
                    "symbol":            symbol,
                    "signal":            "SELL",
                    "decision":          decision_label,
                    "shares":            shares,
                    "entry_price":       entry_price,
                    "stop_price":        0.0,
                    "take_profit_price": 0.0,
                    "position_value":    shares * entry_price,
                    "reject_reason":     reject_reason,
                    "decided_at":        datetime.now(timezone.utc).replace(tzinfo=None),
                })
            except Exception as exc:
                log.warning("Could not persist CLOSED_TIMEOUT for %s: %s", symbol, exc)
            if closed_ok:
                timed_out += 1
                log.info(
                    "[%s] CLOSED_TIMEOUT — %d shares, last BUY %s (%dd ago > %dd limit)",
                    symbol, shares, buy_date, age_days, max_days,
                )

        print()
        return timed_out
    except Exception as exc:
        print(f"  ⚠  Phase 3.6 failed: {exc}")
        log.warning("Phase 3.6 failed: %s", exc, exc_info=True)
        print()
        return timed_out
    finally:
        try:
            loop.run_until_complete(ibkr.disconnect())
        except Exception as exc:
            log.warning("IBKR disconnect error after hold-timeout phase: %s", exc)
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def _close_for_timeout(
    ibkr,
    loop,
    symbol: str,
    shares: int,
    open_orders: list[dict],
) -> bool:
    """
    Cancel any open bracket children for ``symbol`` then submit a market sell.

    Mirrors ``OrderManager._cancel_bracket_children`` + ``_submit_market_close``
    inline so Phase 3.6 doesn't depend on OrderManager's stateful setup
    (PositionSizer, PortfolioGuard, CircuitBreaker — none of which apply to
    a forced close).  Returns True iff the market sell was placed.
    """
    targets = [
        o for o in open_orders
        if o.get("symbol") == symbol
        and o.get("action") == "SELL"
        and o.get("order_type") in ("LMT", "STP", "STP LMT", "TRAIL")
    ]
    for o in targets:
        order_id = o.get("order_id")
        try:
            loop.run_until_complete(ibkr.cancel_order(order_id))
            log.info(
                "[%s] Cancelled bracket child %s id=%s before timeout close",
                symbol, o.get("order_type"), order_id,
            )
        except Exception as exc:
            log.error(
                "[%s] Could not cancel bracket child id=%s: %s — "
                "orphan order may remain live in IBKR",
                symbol, order_id, exc,
            )

    try:
        loop.run_until_complete(ibkr.place_market_order(symbol, "SELL", shares))
        log.info("[%s] Hold-timeout market close submitted (%d shares)", symbol, shares)
        return True
    except Exception as exc:
        log.error("[%s] Hold-timeout market close failed: %s", symbol, exc)
        return False


def _phase4_risk_orders(
    actionable: list[tuple],
    equity: float,
    run_id: str,
    dry_run: bool,
) -> tuple[int, int, int, int, int, int]:
    """
    Run OrderManager for each actionable signal.
    Returns (approved, dry_run_logged, rejected, skipped_duplicates,
    skipped_pending_orders, longs_closed).

    When `dry_run=False` and paper/live mode is active, an IBKRConnection is
    opened for the duration of Phase 4 and passed to OrderManager (along with
    its event loop).  If the connect fails, Phase 4 falls back to dry-run mode
    so the decisions are still recorded for Page 8 / the dashboard.

    Within-session deduplication: EQUIVALENT_PAIRS symbols that share an
    underlying company (e.g. GOOG / GOOGL) are tracked via `decided_symbols`.
    If one member of a pair has already been decided this run, the other is
    skipped (no DB record written) to avoid double-sizing the same position.
    """
    print("=== Phase 4: Risk & order management ===")
    if not actionable:
        print("  No actionable signals.")
        print()
        return 0, 0, 0, 0, 0, 0

    ibkr, loop = _connect_ibkr_if_needed(dry_run)
    effective_dry_run = dry_run

    if not dry_run and ibkr is None:
        # Caller wanted real orders but IBKR is unavailable — degrade to
        # dry-run so decisions are logged rather than spamming REJECTED rows.
        print("  ⚠  IBKR unreachable — falling back to dry-run for this phase.")
        log.error(
            "IBKR connection unavailable; Phase 4 running in dry-run fallback."
        )
        effective_dry_run = True
    elif ibkr is not None:
        print("  ✓  IBKR connected — orders will be submitted.")

    try:
        manager = OrderManager(
            ibkr_connection=ibkr,
            dry_run=effective_dry_run,
            event_loop=loop,
        )
        positions = _fetch_positions(ibkr, loop)
        if positions:
            print(f"  IBKR positions: {sorted(positions.keys())}")

        # Symbols with an unfilled entry bracket still working at IBKR.  Skipping
        # these prevents Phase 4 from stacking a duplicate bracket on a symbol
        # whose prior entry hasn't filled (PortfolioGuard only sees *filled*
        # positions, so it can't catch this).
        pending_entries = _fetch_pending_entry_symbols(ibkr, loop, positions)
        if pending_entries:
            print(f"  Pending entry orders (skipping new signals): {sorted(pending_entries)}")

        decided_symbols: set[str] = set()
        approved               = 0
        dry_run_logged         = 0
        rejected               = 0
        skipped_duplicates     = 0
        skipped_pending_orders = 0
        longs_closed           = 0

        for signal_result, atr in actionable:
            sym = signal_result.symbol

            # Check whether this symbol or its equivalent was already decided.
            equivalent = EQUIVALENT_PAIRS.get(sym)
            if sym in decided_symbols or (equivalent and equivalent in decided_symbols):
                skipped_duplicates += 1
                log.info(
                    "Skipping %s — equivalent symbol already decided this run", sym
                )
                print(f"  {sym}: SKIPPED (equivalent to already-decided symbol)")
                continue

            # Skip symbols (or their GOOG/GOOGL equivalent) that already have an
            # unfilled entry bracket working at IBKR — submitting again would
            # stack a duplicate bracket on the same name.
            if sym in pending_entries or (equivalent and equivalent in pending_entries):
                skipped_pending_orders += 1
                log.info(
                    "Skipping %s — unfilled entry order already working at IBKR", sym
                )
                print(f"  {sym}: SKIPPED (open entry order already working at IBKR)")
                continue

            decision = manager.process(
                signal_result=signal_result,
                equity=equity,
                positions=positions,
                atr=atr,
                run_id=run_id,
            )
            decided_symbols.add(sym)

            if decision.decision == "REJECTED":
                rejected += 1
                print(f"  {decision.symbol}: REJECTED — {decision.reject_reason}")
            elif decision.decision == "REJECTED_TOO_SMALL":
                rejected += 1
                print(f"  {decision.symbol}: REJECTED_TOO_SMALL — {decision.reject_reason}")
            elif decision.decision == "REJECTED_NO_POSITION":
                # Long-only gate intercepts SELL-from-flat; still a rejection so
                # signal_runner_log.orders_rejected reflects what `order_decisions`
                # shows (Page 8 / daily-review parity).
                rejected += 1
                print(f"  {decision.symbol}: {decision.reject_reason}")
            elif decision.decision == "CLOSED_LONG":
                longs_closed += 1
                print(
                    f"  {decision.symbol}: CLOSED_LONG "
                    f"{decision.shares} shares @ {decision.entry_price:.2f}"
                )
            elif decision.decision == "DRY_RUN":
                dry_run_logged += 1
                print(
                    f"  {decision.symbol}: DRY_RUN {decision.signal} "
                    f"{decision.shares} shares @ {decision.entry_price:.2f}"
                )
            else:  # APPROVED
                approved += 1
                print(
                    f"  {decision.symbol}: APPROVED {decision.signal} "
                    f"{decision.shares} shares @ {decision.entry_price:.2f}"
                )

        print()
        return (
            approved, dry_run_logged, rejected,
            skipped_duplicates, skipped_pending_orders, longs_closed,
        )
    finally:
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


def _phase5_summary(
    run_id: str,
    symbols: list[str],
    signals_generated: int,
    approved: int,
    dry_run_logged: int,
    rejected: int,
    skipped_duplicates: int,
    skipped_stale: int,
    skipped_pending_orders: int,
    longs_closed: int,
    trailing_conversions: int,
    hold_timeouts: int,
    duration: float,
    dry_run: bool,
) -> None:
    """Print summary and persist to signal_runner_log."""
    print("=== Phase 5: Summary ===")
    print(f"  Run ID:                {run_id}")
    print(f"  Symbols processed:     {len(symbols)}")
    print(f"  Signals generated:     {signals_generated}")
    print(f"  Orders approved:       {approved}")
    print(f"  Dry-run logged:        {dry_run_logged}")
    print(f"  Orders rejected:       {rejected}")
    print(f"  Longs closed:          {longs_closed}")
    print(f"  Skipped duplicates:    {skipped_duplicates}")
    print(f"  Skipped pending orders:{skipped_pending_orders}")
    print(f"  Skipped stale:         {skipped_stale}")
    print(f"  Trailing conversions:  {trailing_conversions}")
    print(f"  Hold timeouts:         {hold_timeouts}")
    print(f"  Duration:              {duration:.1f}s")

    mode: str
    if dry_run:
        mode = "dry_run"
    elif config.trading.mode == TradingMode.SIMULATION:
        mode = "paper" if config.trading.paper_orders_enabled else "dry_run"
    else:
        mode = "live"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        log_signal_runner_run({
            "run_id":                run_id,
            "run_date":              now.strftime("%Y-%m-%d"),
            "mode":                  mode,
            "symbols_processed":     len(symbols),
            "signals_generated":     signals_generated,
            "orders_submitted":      approved + dry_run_logged,
            "orders_rejected":       rejected,
            "skipped_duplicates":    skipped_duplicates,
            "skipped_pending_orders": skipped_pending_orders,
            "skipped_stale":         skipped_stale,
            "longs_closed":          longs_closed,
            "trailing_conversions":  trailing_conversions,
            "hold_timeouts":         hold_timeouts,
            "duration_seconds":      duration,
            "recorded_at":           now,
            "notes":                 None,
        })
    except Exception as exc:
        log.warning("Could not persist signal runner log: %s", exc)

    print()


# ── Main run ──────────────────────────────────────────────────────────────────

def run(dry_run: bool = True, symbol_filter: str = "") -> None:
    """Execute one full signal-runner cycle."""
    run_id    = str(uuid.uuid4())
    t_start   = time.monotonic()
    equity    = config.trading.paper_equity

    symbols, halted, _ = _phase1_startup(dry_run, symbol_filter)

    if halted:
        # Still log the aborted run
        _phase5_summary(run_id, symbols, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, dry_run)
        return

    _phase2_refresh(symbols)
    actionable, skipped_stale = _phase3_signals(symbols)
    trailing_conversions = _phase3_5_trailing_stops(dry_run, run_id=run_id)
    hold_timeouts = _phase3_6_hold_timeouts(dry_run, run_id=run_id)
    (
        approved, dry_run_logged, rejected,
        skipped_duplicates, skipped_pending_orders, longs_closed,
    ) = _phase4_risk_orders(actionable, equity, run_id, dry_run)

    duration = time.monotonic() - t_start
    _phase5_summary(
        run_id=run_id,
        symbols=symbols,
        signals_generated=len(actionable),
        approved=approved,
        dry_run_logged=dry_run_logged,
        rejected=rejected,
        skipped_duplicates=skipped_duplicates,
        skipped_pending_orders=skipped_pending_orders,
        skipped_stale=skipped_stale,
        longs_closed=longs_closed,
        trailing_conversions=trailing_conversions,
        hold_timeouts=hold_timeouts,
        duration=duration,
        dry_run=dry_run,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Daily signal runner")
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
        help="Actually submit orders (requires paper_orders_enabled=True or LIVE mode)",
    )
    parser.add_argument(
        "--symbol",
        default="",
        metavar="SYM",
        help="Process a single symbol instead of the full list",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run forever, firing at 09:35 each weekday",
    )
    args = parser.parse_args()

    if args.schedule:
        import schedule as sched

        def _job():
            print(f"\n{'='*60}")
            print(f"  Signal Runner — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            print(f"{'='*60}\n")
            run(dry_run=args.dry_run, symbol_filter=args.symbol)

        for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            getattr(sched.every(), day).at("09:35").do(_job)

        print("Signal runner scheduled at 09:35 Mon-Fri.  Press Ctrl+C to stop.")
        try:
            while True:
                sched.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            print("Signal runner stopped.")
    else:
        print(f"\n{'='*60}")
        print(f"  Signal Runner — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}\n")
        run(dry_run=args.dry_run, symbol_filter=args.symbol)


if __name__ == "__main__":
    main()
