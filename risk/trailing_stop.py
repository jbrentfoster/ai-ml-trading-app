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
from typing import Optional

from config.settings import config
from core.logger import get_logger

log = get_logger("risk.trailing_stop")


@dataclass
class TrailingStopAction:
    """One row per position evaluated during a trailing-stop cycle.

    Price/ATR fields are Optional so that paths which legitimately don't have
    a measurement (e.g. the "trailing stop already active" idempotency branch,
    where re-fetching ATR would be wasted I/O) can write NULL to
    trailing_stop_log instead of misleading 0.0 values that quietly skew any
    dashboard aggregation.
    """
    symbol:        str
    action:        str        # "CONVERTED" | "SKIPPED" | "FAILED"
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

    def manage(self, run_id: str = "") -> list[TrailingStopAction]:
        """
        Walk current long positions and convert qualifying bracket TPs into
        trailing stops.  Returns one action record per position evaluated.

        Each action is persisted to `trailing_stop_log` so Page 8 can surface a
        retrospective view (action, entry/current/ATR, trail distance, reason).
        Failures in the persist step are swallowed — logging a row is less
        important than completing the IBKR conversions cleanly.
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
            action = self._evaluate_position(pos, shares, open_orders)
            if action is not None:
                action.run_id = run_id
                actions.append(action)
                self._persist(action)
        return actions

    # ── Per-position evaluation ───────────────────────────────────────────────

    def _evaluate_position(
        self, pos: dict, shares: int, open_orders: list[dict]
    ) -> Optional[TrailingStopAction]:
        symbol = pos["symbol"]

        # 1. Idempotency — skip if a TRAIL order is already open.
        if any(
            o.get("symbol") == symbol and o.get("order_type") == "TRAIL"
            for o in open_orders
        ):
            # Populate the cheap-to-fetch fields so the trailing_stop_log row
            # is informative ("we're at $X, entry was $Y") rather than
            # all-zeros.  atr / trail_amount stay None — the trail was sized
            # at the moment of conversion in a prior run; the current ATR is
            # not what's protecting this position anymore, and writing
            # today's value would be misleading.
            entry_price   = float(pos.get("avg_cost", 0.0) or 0.0)
            current_price = self._get_latest_close(symbol)
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

        # 3. Latest ATR.
        from data.database import get_latest_indicators
        ind = get_latest_indicators(symbol, "1d")
        atr = float(ind["atr_14"]) if ind and ind.get("atr_14") else 0.0
        if atr <= 0:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                reason="no ATR available",
            )

        # 4. Current price — use latest daily close from SQLite (no live call
        # to keep this fast and deterministic).  ATR is daily anyway, so a
        # daily close is the right comparison point.
        current_price = self._get_latest_close(symbol)
        entry_price   = float(pos.get("avg_cost", 0.0) or 0.0)
        if current_price <= 0 or entry_price <= 0:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price, current_price=current_price, atr=atr,
                reason="no valid price data",
            )

        # 5. Activation threshold.
        activation_profit = self._cfg.trailing_stop_activation_atr * atr
        threshold         = entry_price + activation_profit
        if current_price < threshold:
            return TrailingStopAction(
                symbol=symbol, action="SKIPPED", shares=shares,
                entry_price=entry_price, current_price=current_price, atr=atr,
                reason=(
                    f"price {current_price:.2f} below activation "
                    f"{threshold:.2f} (entry {entry_price:.2f} + "
                    f"{self._cfg.trailing_stop_activation_atr:.1f}×ATR)"
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
