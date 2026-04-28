"""
Order manager — orchestrates the full signal → position-size → guard → order lifecycle.

Decision outcomes:
  DRY_RUN              — dry_run=True, or SIMULATION mode without paper_orders_enabled.
                         The decision is logged but no order is sent to IBKR.
  REJECTED             — PortfolioGuard blocked the trade.
  REJECTED_TOO_SMALL   — PositionSizer returned shares < 1 (position_value < entry_price,
                         or entry_price unavailable).  No order placed, guard bypassed.
  APPROVED             — Trade passed all checks.  If an IBKR connection is available,
                         a bracket order (entry + stop + TP) is submitted.
  CLOSED_LONG          — SELL signal on an existing long position.  A market sell order
                         is placed to close the full position (long-only mode).
  REJECTED_NO_POSITION — SELL signal when no long position is held and short selling is
                         disabled (allow_short_selling=False).  No order is placed.

Long-only SELL behaviour (allow_short_selling=False, the default):
  SELL on held long  → CLOSED_LONG  (market sell to close)
  SELL with no long  → REJECTED_NO_POSITION  (nothing to close; no short opened)

Bracket order (BUY / future short SELL):
  Entry: market order
  Stop:  stop-limit at stop_price
  TP:    limit order at take_profit_price
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import config, TradingMode
from core.logger import get_logger
from data.database import log_order_decision
from models.signal_gate import SignalResult
from risk.circuit_breaker import CircuitBreaker
from risk.portfolio_guard import PortfolioGuard
from risk.position_sizer import PositionSizer

log = get_logger("risk.order_manager")


@dataclass
class OrderDecision:
    symbol:            str
    signal:            str       # "BUY" | "SELL"
    decision:          str       # "APPROVED" | "REJECTED" | "REJECTED_TOO_SMALL" | "DRY_RUN" | "CLOSED_LONG" | "REJECTED_NO_POSITION"
    shares:            int       = 0
    entry_price:       float     = 0.0
    stop_price:        float     = 0.0
    take_profit_price: float     = 0.0
    position_value:    float     = 0.0
    reject_reason:     str       = ""
    decided_at:        datetime  = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    run_id:            str       = ""


class OrderManager:

    def __init__(
        self,
        ibkr_connection=None,   # IBKRConnection instance or None
        dry_run: bool = False,
        event_loop=None,        # asyncio loop the IBKRConnection was created on
    ) -> None:
        self._ibkr     = ibkr_connection
        self._dry_run  = dry_run
        self._loop     = event_loop
        self._sizer    = PositionSizer()
        self._cb       = CircuitBreaker()
        self._guard    = PortfolioGuard(circuit_breaker=self._cb)
        self._trading  = config.trading

    def process(
        self,
        signal_result: SignalResult,
        equity: float,
        positions: dict,
        atr: float | None = None,
        daily_pnl_pct: float = 0.0,
        run_id: str = "",
    ) -> OrderDecision:
        """
        Full lifecycle for one signal:
          1. Position sizing (Kelly / fixed)
          2. Portfolio guard (6 checks)
          3. Order submission (or dry-run log)

        `positions` maps symbol → dict with keys: shares, entry_price, current_price.
        `daily_pnl_pct` is today's portfolio return as a fraction (e.g. -0.015).
        """
        symbol = signal_result.symbol
        signal = signal_result.signal  # "BUY" | "SELL"

        # ── Determine mode ────────────────────────────────────────────────────
        is_dry_run = (
            self._dry_run
            or (
                self._trading.mode == TradingMode.SIMULATION
                and not self._trading.paper_orders_enabled
            )
        )

        # ── Long-only SELL handling ───────────────────────────────────────────
        # When short selling is disabled (default), a SELL signal either closes
        # an existing long or is rejected if there is nothing to close.
        if signal == "SELL" and not self._trading.allow_short_selling:
            return self._handle_long_only_sell(
                symbol=symbol,
                signal_result=signal_result,
                positions=positions,
                run_id=run_id,
            )

        # ── Position sizing ───────────────────────────────────────────────────
        # Use latest close from bars as entry price if not provided
        entry_price = self._get_latest_close(symbol)
        pos_size = self._sizer.calculate(
            symbol=symbol,
            signal=signal,
            equity=equity,
            entry_price=entry_price,
            atr=atr,
        )

        # ── Minimum-size reject ───────────────────────────────────────────────
        # shares < 1 means either position_value < entry_price (Kelly sized us
        # out) or entry_price was 0 (no bars available).  Either way, don't
        # forward a 0-share "approved" decision to the guard or IBKR.
        if pos_size.shares < 1:
            reason = (
                f"Position size below 1 share — "
                f"value={pos_size.position_value:.2f}, entry={pos_size.entry_price:.2f}"
            )
            decision = OrderDecision(
                symbol=symbol,
                signal=signal,
                decision="REJECTED_TOO_SMALL",
                shares=0,
                entry_price=pos_size.entry_price,
                stop_price=pos_size.stop_price,
                take_profit_price=pos_size.take_profit_price,
                position_value=pos_size.position_value,
                reject_reason=reason,
                run_id=run_id,
            )
            self._persist(decision)
            log.info("[%s] REJECTED_TOO_SMALL — %s", symbol, reason)
            return decision

        # ── Portfolio guard ───────────────────────────────────────────────────
        guard_result = self._guard.check(
            symbol=symbol,
            signal=signal,
            position_size=pos_size,
            equity=equity,
            positions=positions,
            daily_pnl_pct=daily_pnl_pct,
        )

        if not guard_result.passed:
            decision = OrderDecision(
                symbol=symbol,
                signal=signal,
                decision="REJECTED",
                shares=pos_size.shares,
                entry_price=pos_size.entry_price,
                stop_price=pos_size.stop_price,
                take_profit_price=pos_size.take_profit_price,
                position_value=pos_size.position_value,
                reject_reason=guard_result.reason,
                run_id=run_id,
            )
            self._persist(decision)
            log.info("[%s] REJECTED — %s", symbol, guard_result.reason)
            return decision

        # ── Dry-run or live/paper order ───────────────────────────────────────
        if is_dry_run:
            decision = OrderDecision(
                symbol=symbol,
                signal=signal,
                decision="DRY_RUN",
                shares=pos_size.shares,
                entry_price=pos_size.entry_price,
                stop_price=pos_size.stop_price,
                take_profit_price=pos_size.take_profit_price,
                position_value=pos_size.position_value,
                reject_reason="",
                run_id=run_id,
            )
            self._persist(decision)
            log.info(
                "[%s] DRY_RUN %s %d shares @ %.2f | stop=%.2f tp=%.2f",
                symbol, signal, pos_size.shares, pos_size.entry_price,
                pos_size.stop_price, pos_size.take_profit_price,
            )
            return decision

        # ── Submit bracket order via IBKR ─────────────────────────────────────
        # Reaching this point means is_dry_run=False (caller wants real submission)
        # AND the portfolio guard passed.  If no IBKR connection was provided,
        # the caller bypassed the dry-run gate but didn't wire up a broker —
        # that's a configuration error, not a dry-run outcome.  Log it as
        # REJECTED so the counters and dashboard reflect reality.
        if self._ibkr is None:
            decision = OrderDecision(
                symbol=symbol,
                signal=signal,
                decision="REJECTED",
                shares=pos_size.shares,
                entry_price=pos_size.entry_price,
                stop_price=pos_size.stop_price,
                take_profit_price=pos_size.take_profit_price,
                position_value=pos_size.position_value,
                reject_reason="IBKR connection unavailable",
                run_id=run_id,
            )
            self._persist(decision)
            log.error(
                "[%s] REJECTED — IBKR connection unavailable (dry_run=False but no ibkr passed)",
                symbol,
            )
            return decision

        submitted = self._submit_bracket_order(
            symbol=symbol,
            signal=signal,
            shares=pos_size.shares,
            entry_price=pos_size.entry_price,
            stop_price=pos_size.stop_price,
            tp_price=pos_size.take_profit_price,
        )

        decision = OrderDecision(
            symbol=symbol,
            signal=signal,
            decision="APPROVED" if submitted else "REJECTED",
            shares=pos_size.shares,
            entry_price=pos_size.entry_price,
            stop_price=pos_size.stop_price,
            take_profit_price=pos_size.take_profit_price,
            position_value=pos_size.position_value,
            reject_reason="" if submitted else "IBKR submission failed",
            run_id=run_id,
        )
        self._persist(decision)
        log.info(
            "[%s] %s %s %d shares @ %.2f",
            symbol, decision.decision, signal, pos_size.shares, pos_size.entry_price,
        )
        return decision

    # ── Private helpers ───────────────────────────────────────────────────────

    def _handle_long_only_sell(
        self,
        symbol: str,
        signal_result: SignalResult,
        positions: dict,
        run_id: str,
    ) -> OrderDecision:
        """
        Handle a SELL signal in long-only mode (allow_short_selling=False).

        If an existing long position is found in `positions`, close it via a
        market sell order and return decision='CLOSED_LONG'.

        If no long is held, return decision='REJECTED_NO_POSITION' — no order
        is placed and no short position is opened.
        """
        pos = positions.get(symbol, {})
        shares = int(pos.get("shares", 0)) if pos else 0

        if shares > 0:
            return self._close_long_position(
                symbol=symbol,
                shares=shares,
                signal_result=signal_result,
                run_id=run_id,
            )

        # No long held — nothing to close.
        decision = OrderDecision(
            symbol=symbol,
            signal=signal_result.signal,
            decision="REJECTED_NO_POSITION",
            shares=0,
            entry_price=0.0,
            stop_price=0.0,
            take_profit_price=0.0,
            position_value=0.0,
            reject_reason="SELL signal ignored — no long position held (short selling not enabled)",
            run_id=run_id,
        )
        self._persist(decision)
        log.info("[%s] REJECTED_NO_POSITION — no long held, short selling disabled", symbol)
        return decision

    def _close_long_position(
        self,
        symbol: str,
        shares: int,
        signal_result: SignalResult,
        run_id: str,
    ) -> OrderDecision:
        """
        Close an existing long position with a market sell order.
        Returns decision='CLOSED_LONG'.  In dry-run mode the order is simulated.
        """
        entry_price = self._get_latest_close(symbol)

        is_dry_run = (
            self._dry_run
            or (
                self._trading.mode == TradingMode.SIMULATION
                and not self._trading.paper_orders_enabled
            )
        )

        if not is_dry_run and self._ibkr is not None:
            self._submit_market_close(symbol, shares)

        decision = OrderDecision(
            symbol=symbol,
            signal=signal_result.signal,
            decision="CLOSED_LONG",
            shares=shares,
            entry_price=entry_price,
            stop_price=0.0,
            take_profit_price=0.0,
            position_value=shares * entry_price,
            reject_reason="",
            run_id=run_id,
        )
        self._persist(decision)
        log.info(
            "[%s] CLOSED_LONG — %d shares @ %.2f (dry_run=%s)",
            symbol, shares, entry_price, is_dry_run,
        )
        return decision

    def _submit_market_close(self, symbol: str, shares: int) -> bool:
        """
        Place a market sell order via IBKRConnection to close a long position.
        Returns True on success, False on any error.

        Reuses self._loop (the loop the IBKRConnection was created on) when
        provided — avoids the "Event loop is closed" error that occurs when
        a fresh loop is used against an IB client bound to a different loop.
        """
        try:
            coro = self._ibkr.place_market_order(symbol, "SELL", shares)
            if self._loop is not None:
                self._loop.run_until_complete(coro)
            else:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(coro)
                finally:
                    loop.close()
            log.info("Market close order submitted for %s (%d shares)", symbol, shares)
            return True
        except Exception as exc:
            log.error("Market close order failed for %s: %s", symbol, exc)
            return False

    def _get_latest_close(self, symbol: str) -> float:
        """Return the most recent close price from SQLite, or 0.0 if unavailable."""
        try:
            from data.database import get_bars
            bars = get_bars(symbol, "1d", limit=1)
            if not bars.empty:
                return float(bars["Close"].iloc[-1])
        except Exception:
            pass
        return 0.0

    def _persist(self, decision: OrderDecision) -> None:
        """Write the decision to order_decisions table."""
        try:
            log_order_decision({
                "run_id":            decision.run_id,
                "symbol":            decision.symbol,
                "signal":            decision.signal,
                "decision":          decision.decision,
                "shares":            decision.shares,
                "entry_price":       decision.entry_price,
                "stop_price":        decision.stop_price,
                "take_profit_price": decision.take_profit_price,
                "position_value":    decision.position_value,
                "reject_reason":     decision.reject_reason,
                "decided_at":        decision.decided_at,
            })
        except Exception as exc:
            log.warning("Could not persist order decision for %s: %s", decision.symbol, exc)

    def _submit_bracket_order(
        self,
        symbol: str,
        signal: str,
        shares: int,
        entry_price: float,
        stop_price: float,
        tp_price: float,
    ) -> bool:
        """
        Attempt to submit a bracket order via IBKRConnection.
        Returns True on success, False on any error.
        """
        try:
            action = "BUY" if signal == "BUY" else "SELL"

            coro = self._ibkr.place_bracket_order(
                symbol=symbol,
                action=action,
                quantity=shares,
                entry_price=entry_price,
                stop_loss_price=stop_price,
                take_profit_price=tp_price,
            )

            # Reuse self._loop when provided so ib_insync's IB client (bound to
            # that loop at connect time) doesn't error on a fresh loop.
            if self._loop is not None:
                trade = self._loop.run_until_complete(coro)
            else:
                loop = asyncio.new_event_loop()
                try:
                    trade = loop.run_until_complete(coro)
                finally:
                    loop.close()

            log.info("Bracket order submitted for %s: %s", symbol, trade)
            return True
        except Exception as exc:
            log.error("Bracket order submission failed for %s: %s", symbol, exc)
            return False
