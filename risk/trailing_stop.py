"""
Trailing stop manager
=====================
Converts the LMT take-profit leg of a bracketed long into a standalone GTC
TRAIL order once the position has moved favourably by at least
`trailing_stop_activation_atr × ATR`.  The original STP and TP legs are both
cancelled; the new TRAIL becomes the sole exit, ratcheting upward as price
rises and triggering on a reversal of `trailing_stop_trail_atr × ATR`.

Conversion sequence (Cancel-TP → Cancel-STP → Submit-TRAIL) is intentional:
  * Cancelling TP first removes the upside cap without losing stop protection.
  * Cancelling STP second leaves a sub-second window of "no stop" — acceptable
    in normal markets.
  * If the TRAIL submission fails after STP is cancelled, we log an error and
    the position is unprotected — the caller should alert ops.

An alternative "Submit-TRAIL first, cancel later" order would create a more
dangerous failure mode: if STP fires while TRAIL is still up (different OCA
groups), a severe drop could trigger both and leave the account short.

Idempotent — positions that already have a TRAIL order open are skipped.

Long-only.  Short positions are skipped for now (allow_short_selling is False
by default in this codebase).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from config.settings import config
from core.logger import get_logger

log = get_logger("risk.trailing_stop")

# Sub-cent moves are ignored when comparing a logged trail trigger against the
# live Order.trailStopPrice — IBKR rounds to the contract tick (1¢ for US
# equities) and a comparison tighter than the tick would generate spurious
# RATCHETED rows on floating-point noise alone.
_RATCHET_EPSILON: float = 0.01


@dataclass
class TrailingStopAction:
    """One row per position evaluated during a trailing-stop cycle.

    Price/ATR fields are Optional so that paths which legitimately don't have
    a measurement (e.g. the "trailing stop already active" idempotency branch,
    where re-fetching ATR would be wasted I/O) can write NULL to
    trailing_stop_log instead of misleading 0.0 values that quietly skew any
    dashboard aggregation.

    ``action`` values:
      * "CONVERTED" — bracket TP→TRAIL conversion succeeded this run
      * "RATCHETED" — TRAIL was already active and IBKR has ratcheted the
                      stop trigger up since the last log entry for this symbol
                      (intraday-runner observability; not a state change we
                      caused)
      * "SKIPPED"   — evaluated but no action taken (already active without
                      ratchet, below activation, no ATR, manual position, etc.)
      * "FAILED"    — conversion attempted but a cancel or submit raised
    """
    symbol:        str
    action:        str        # "CONVERTED" | "RATCHETED" | "SKIPPED" | "FAILED"
    shares:        int                  = 0
    entry_price:   Optional[float]      = None
    current_price: Optional[float]      = None
    atr:           Optional[float]      = None
    trail_amount:  Optional[float]      = None
    reason:        str                  = ""
    run_id:        str                  = ""
    timestamp:     datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )


class TrailingStopManager:
    """
    Parameters
    ----------
    ibkr_connection :
        Live IBKRConnection.  Must be connected when `manage()` is called.
    event_loop :
        The asyncio loop the IBKRConnection was created on — required so
        ib_insync's IB client isn't used from a foreign loop.
    """

    def __init__(self, ibkr_connection, event_loop) -> None:
        self._ibkr = ibkr_connection
        self._loop = event_loop

    # Access config.risk at call time rather than capturing in __init__ so
    # test-time `patch("risk.trailing_stop.config")` swaps are respected.
    @property
    def _cfg(self):
        return config.risk

    # ── Public API ────────────────────────────────────────────────────────────

    def manage(
        self,
        run_id: str = "",
        *,
        price_source: Optional[Callable[[str], float]] = None,
        intraday: bool = False,
    ) -> list[TrailingStopAction]:
        """
        Walk current long positions and convert qualifying bracket TPs into
        trailing stops.  Returns one action record per position evaluated.

        Each action is persisted to `trailing_stop_log` so Page 8 can surface a
        retrospective view (action, entry/current/ATR, trail distance, reason).
        Failures in the persist step are swallowed — logging a row is less
        important than completing the IBKR conversions cleanly.

        Parameters
        ----------
        run_id :
            Persisted to ``trailing_stop_log.run_id`` for run-scoped queries.
        price_source :
            Optional sync callable mapping symbol → current price.  When
            ``None`` (the default — preserves existing daily-runner behavior),
            current price is read from ``ohlcv_bars`` via ``get_bars(..., limit=1)``.
            When provided, ``price_source(symbol)`` is called for each evaluated
            position.  The intraday runner passes a wrapper around
            ``IBKRConnection.get_last_price()`` so mid-day evaluations use the
            live IBKR quote instead of yesterday's stored close.

            ATR continues to come from ``indicator_snapshots`` regardless —
            it is a daily-bar-derived value and does not change intraday.  The
            trail distance computed when a conversion fires is therefore
            "ATR-as-of-last-completed-bar", not "ATR-as-of-now".
        intraday :
            Signals that this is an intraday cadence run (not the 09:35 ET
            daily runner).  When True, two gates engage:

              * If ``config.risk.intraday_trail_conversion_enabled`` is False
                (the default), bracket→TRAIL conversions are suppressed —
                this run only logs ratchet events and SKIPPED rows.
              * If conversions are enabled, the activation threshold is
                tightened by ``config.risk.intraday_conversion_buffer_atr ×
                ATR`` on top of the daily ``trailing_stop_activation_atr ×
                ATR`` requirement.  The buffer keeps mid-day conversions to
                genuinely-strong moves where the cancel+place "no stop"
                window is least risky.
        """
        if not self._cfg.trailing_stop_enabled:
            log.debug("Trailing stops disabled in config; skipping")
            return []

        if self._ibkr is None or self._loop is None:
            log.warning("Trailing stop manager requires a live IBKR connection")
            return []

        try:
            positions   = self._loop.run_until_complete(self._ibkr.get_positions())
            open_orders = self._loop.run_until_complete(self._ibkr.get_open_orders())
        except Exception as exc:
            log.error("Could not fetch IBKR state for trailing stops: %s", exc)
            return []

        actions: list[TrailingStopAction] = []
        for pos in positions:
            shares = int(pos.get("quantity", 0) or 0)
            if shares <= 0:
                # Long-only codepath.  Surface the skip explicitly so an
                # unexpected short (e.g. an orphan bracket leg that fired
                # after a market close) shows up in the daily log instead
                # of vanishing silently.
                action = TrailingStopAction(
                    symbol=pos.get("symbol", "?"),
                    action="SKIPPED",
                    shares=shares,
                    reason=(
                        f"non-long position (quantity={shares}) — "
                        "long-only trailing stops"
                    ),
                )
                action.run_id = run_id
                actions.append(action)
                self._persist(action)
                continue
            action = self._evaluate_position(
                pos, shares, open_orders,
                price_source=price_source, intraday=intraday,
            )
            if action is not None:
                action.run_id = run_id
                actions.append(action)
                self._persist(action)
        return actions

    # ── Per-position evaluation ───────────────────────────────────────────────

    def _evaluate_position(
        self,
        pos: dict,
        shares: int,
        open_orders: list[dict],
        *,
        price_source: Optional[Callable[[str], float]] = None,
        intraday: bool = False,
    ) -> Optional[TrailingStopAction]:
        symbol = pos["symbol"]

        # 1. Idempotency — if a TRAIL order is already open, this is either a
        #    no-op (already converted; nothing to do) or a ratchet event worth
        #    logging.  Capture the TRAIL leg so we can read its live trigger
        #    (Order.trailStopPrice) for ratchet detection.
        existing_trail = next(
            (o for o in open_orders
             if o.get("symbol") == symbol and o.get("order_type") == "TRAIL"),
            None,
        )
        if existing_trail is not None:
            entry_price   = float(pos.get("avg_cost", 0.0) or 0.0)
            current_price = self._resolve_current_price(symbol, price_source)
            # auxPrice (trail distance) is exposed by IBKRConnection.get_open_orders
            # as the ``stop_price`` field on TRAIL rows.  It doesn't change after
            # conversion — only the trigger ratchets.
            trail_distance = self._safe_float(existing_trail.get("stop_price"))
            live_trigger   = self._safe_float(existing_trail.get("trail_stop_price"))

            ratcheted, prior_trigger = self._detect_ratchet(symbol, live_trigger)
            if ratcheted:
                reason = (
                    f"ratcheted: trigger ${prior_trigger:.2f} → ${live_trigger:.2f}"
                    if prior_trigger is not None and live_trigger is not None
                    else "ratcheted (no prior trigger logged)"
                )
                return TrailingStopAction(
                    symbol=symbol, action="RATCHETED", shares=shares,
                    entry_price=entry_price if entry_price > 0 else None,
                    current_price=current_price if current_price > 0 else None,
                    atr=None,
                    trail_amount=trail_distance,
                    reason=reason,
                )
            # No ratchet detected — preserve the existing SKIPPED behavior so
            # backward-compat tests keep passing.  atr / trail_amount stay
            # None on the SKIPPED branch: the trail was sized in a prior run,
            # so today's ATR isn't what's protecting it.
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price if entry_price > 0 else None,
                current_price=current_price if current_price > 0 else None,
                reason="trailing stop already active",
            )

        # 2. Find the bracket legs for this symbol.
        tp_leg = next(
            (o for o in open_orders
             if o.get("symbol") == symbol
             and o.get("action") == "SELL"
             and o.get("order_type") == "LMT"),
            None,
        )
        stp_leg = next(
            (o for o in open_orders
             if o.get("symbol") == symbol
             and o.get("action") == "SELL"
             and o.get("order_type") in ("STP", "STP LMT")),
            None,
        )
        if tp_leg is None or stp_leg is None:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                reason="no bracket TP/STP legs found (manual position?)",
            )

        # 2b. Intraday conversion gate.  When this is an intraday run AND
        # mid-day conversions are not enabled, suppress conversion attempts
        # entirely and log a SKIPPED row (ratchet-only mode for the runner).
        # The two daily Phase 3.5 calls (paths through signal_runner.py) never
        # pass intraday=True so this branch is invisible to them.
        if intraday and not self._cfg.intraday_trail_conversion_enabled:
            entry_price   = float(pos.get("avg_cost", 0.0) or 0.0)
            current_price = self._resolve_current_price(symbol, price_source)
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price if entry_price > 0 else None,
                current_price=current_price if current_price > 0 else None,
                reason="intraday: conversions disabled (ratchet-only)",
            )

        # 3. Latest ATR.
        from data.database import get_latest_indicators
        ind = get_latest_indicators(symbol, "1d")
        atr = float(ind["atr_14"]) if ind and ind.get("atr_14") else 0.0
        if atr <= 0:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                reason="no ATR available",
            )

        # 4. Current price — daily path reads latest daily close from SQLite;
        # intraday path uses the supplied price_source (live IBKR quote).
        # ATR is daily anyway, so the daily close was historically a fine
        # comparison point — but mid-day at 12:00 ET that close is yesterday's
        # value, 18+ hours stale.  See "Intraday Phase 3.5 reads price from
        # IBKR, not the cached daily bar" architectural-decision note.
        current_price = self._resolve_current_price(symbol, price_source)
        entry_price   = float(pos.get("avg_cost", 0.0) or 0.0)
        if current_price <= 0 or entry_price <= 0:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price, current_price=current_price, atr=atr,
                reason="no valid price data",
            )

        # 5. Activation threshold.  Intraday runs add a buffer on top of the
        # daily activation_atr requirement (see intraday_conversion_buffer_atr
        # in RiskConfig).  Only reached when intraday=True AND
        # intraday_trail_conversion_enabled=True (the earlier gate at step 2b
        # short-circuits the disabled case).
        if intraday:
            activation_multiplier = (
                self._cfg.trailing_stop_activation_atr
                + self._cfg.intraday_conversion_buffer_atr
            )
        else:
            activation_multiplier = self._cfg.trailing_stop_activation_atr
        activation_profit = activation_multiplier * atr
        threshold         = entry_price + activation_profit
        if current_price < threshold:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price, current_price=current_price, atr=atr,
                reason=(
                    f"price {current_price:.2f} below activation "
                    f"{threshold:.2f} (entry {entry_price:.2f} + "
                    f"{activation_multiplier:.1f}×ATR)"
                ),
            )

        # 6. Convert: cancel TP → cancel STP → submit TRAIL.
        trail_amount = round(self._cfg.trailing_stop_trail_atr * atr, 2)

        try:
            self._loop.run_until_complete(
                self._ibkr.cancel_order(tp_leg["order_id"])
            )
        except Exception as exc:
            log.error("Could not cancel TP leg for %s: %s", symbol, exc)
            return TrailingStopAction(
                symbol=symbol, action="FAILED", shares=shares,
                entry_price=entry_price, current_price=current_price,
                atr=atr, trail_amount=trail_amount,
                reason=f"cancel TP failed: {exc}",
            )

        try:
            self._loop.run_until_complete(
                self._ibkr.cancel_order(stp_leg["order_id"])
            )
        except Exception as exc:
            log.error(
                "Could not cancel STP leg for %s after cancelling TP: %s — "
                "position may be left without a downside stop until next run",
                symbol, exc,
            )
            return TrailingStopAction(
                symbol=symbol, action="FAILED", shares=shares,
                entry_price=entry_price, current_price=current_price,
                atr=atr, trail_amount=trail_amount,
                reason=f"cancel STP failed: {exc}",
            )

        try:
            self._loop.run_until_complete(
                self._ibkr.place_trailing_stop(
                    symbol=symbol,
                    action="SELL",
                    quantity=shares,
                    trail_amount=trail_amount,
                )
            )
        except Exception as exc:
            log.error(
                "Submit TRAIL failed for %s after cancelling bracket stops — "
                "position is now UNPROTECTED: %s", symbol, exc,
            )
            return TrailingStopAction(
                symbol=symbol, action="FAILED", shares=shares,
                entry_price=entry_price, current_price=current_price,
                atr=atr, trail_amount=trail_amount,
                reason=f"place TRAIL failed: {exc}",
            )

        log.info(
            "Trailing stop activated for %s: entry=%.2f current=%.2f "
            "ATR=%.2f trail=$%.2f (%d shares)",
            symbol, entry_price, current_price, atr, trail_amount, shares,
        )
        return TrailingStopAction(
            symbol=symbol, action="CONVERTED", shares=shares,
            entry_price=entry_price, current_price=current_price,
            atr=atr, trail_amount=trail_amount,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_latest_close(symbol: str) -> float:
        try:
            from data.database import get_bars
            bars = get_bars(symbol, "1d", limit=1)
            if not bars.empty:
                return float(bars["Close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    @classmethod
    def _resolve_current_price(
        cls,
        symbol: str,
        price_source: Optional[Callable[[str], float]],
    ) -> float:
        """Return the price input for activation / ratchet checks.

        Daily path (price_source=None) reads the latest bar from ohlcv_bars.
        Intraday path (price_source supplied) calls the source and tolerates a
        None/0/raising callable by falling back to 0.0 — the caller treats 0
        as "no valid price" and emits a SKIPPED row.
        """
        if price_source is None:
            return cls._get_latest_close(symbol)
        try:
            value = price_source(symbol)
        except Exception as exc:
            log.warning("Custom price_source failed for %s: %s", symbol, exc)
            return 0.0
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """Coerce an ib_insync price field to a usable float or None.

        ib_insync fills unused price fields with sys.float_info.max; the
        ``get_open_orders`` helper already filters those to None for most
        callers, but defend in depth here in case the caller passes raw
        order data.
        """
        if value is None:
            return None
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None
        if fv == 0.0:
            return None
        # Guard against the sys.float_info.max sentinel from ib_insync.
        if fv > 1e100 or fv != fv:  # NaN check via self-inequality
            return None
        return fv

    @classmethod
    def _detect_ratchet(
        cls,
        symbol: str,
        live_trigger: Optional[float],
    ) -> tuple[bool, Optional[float]]:
        """Compare the live IBKR trail trigger against the last logged trigger.

        Returns ``(ratcheted, prior_trigger)``.  ``ratcheted`` is True only
        when both the live trigger and a prior-trigger derivation are present
        AND the live trigger exceeds the prior by more than ``_RATCHET_EPSILON``.

        Prior trigger is derived from the most recent trailing_stop_log row
        for this symbol: ``last.current_price - last.trail_amount``.  When
        either field is missing (e.g. the prior row was SKIPPED from the
        idempotency branch and never populated trail_amount), no comparison
        is possible — return (False, None).  Same for a TRAIL whose
        ``trailStopPrice`` IBKR has not yet reported (live_trigger is None).
        """
        if live_trigger is None:
            return False, None
        try:
            from data.database import get_latest_trailing_stop_log_for_symbol
            last = get_latest_trailing_stop_log_for_symbol(symbol)
        except Exception as exc:
            log.warning("Could not look up trailing_stop_log for %s: %s", symbol, exc)
            return False, None
        if last is None:
            return False, None
        prior_price = last.get("current_price")
        prior_dist  = last.get("trail_amount")
        if prior_price is None or prior_dist is None:
            return False, None
        prior_trigger = float(prior_price) - float(prior_dist)
        if live_trigger > prior_trigger + _RATCHET_EPSILON:
            return True, prior_trigger
        return False, prior_trigger

    @staticmethod
    def _persist(action: TrailingStopAction) -> None:
        try:
            from data.database import log_trailing_stop_action
            log_trailing_stop_action({
                "run_id":        action.run_id,
                "symbol":        action.symbol,
                "action":        action.action,
                "shares":        action.shares,
                "entry_price":   action.entry_price,
                "current_price": action.current_price,
                "atr":           action.atr,
                "trail_amount":  action.trail_amount,
                "reason":        action.reason,
                "decided_at":    action.timestamp,
            })
        except Exception as exc:
            log.warning("Could not persist trailing stop action for %s: %s",
                        action.symbol, exc)
