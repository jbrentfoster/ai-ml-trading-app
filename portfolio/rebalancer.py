"""Execution wrapper — submit a RebalancePlan's trades to IBKR.

Gated by the caller (scripts/rebalance.py enforces the two gates: the
``--no-dry-run`` flag AND ``config.allocation.rebalance_orders_enabled`` — see
docs/strategy/risk_premia_harvesting.md §4).  Uses marketable LIMIT orders
(reference price ± ``slippage_cap``, tick-rounded to $0.01) so slippage is
bounded while liquid ETFs still fill; fractional shares (rounded to
``share_precision`` decimals; 0 = whole shares).

The order loop is kept free of IBKR construction so it can be unit-tested with a
mocked connection — only ``conn.place_limit_order(...)`` is called.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SubmitResult:
    ticker: str
    action: str          # BUY | SELL
    shares: float
    limit_price: float
    status: str          # IBKR order status, or "SKIPPED (...)" / "FAILED: ..."
    order_id: int | None = None

    @property
    def ok(self) -> bool:
        return not (self.status.startswith("SKIPPED") or self.status.startswith("FAILED"))


def marketable_limit(action: str, ref_price: float, slippage_cap: float) -> float:
    """Reference price nudged across the spread by ``slippage_cap``, tick-rounded.
    BUY pays up to +cap, SELL accepts down to −cap — bounding worst-case slippage."""
    px = ref_price * (1 + slippage_cap) if action.upper() == "BUY" else ref_price * (1 - slippage_cap)
    return round(px, 2)


async def submit_plan(conn, plan, *, slippage_cap: float = 0.005,
                      share_precision: int = 4) -> list[SubmitResult]:
    """Submit every non-HOLD proposal in ``plan`` as a marketable limit order.

    Errors on a single order are caught and recorded (FAILED) so one bad order
    never aborts the rest of the rebalance.  Returns one SubmitResult per trade.
    """
    results: list[SubmitResult] = []
    for p in plan.trades:                      # HOLDs are already excluded
        qty = round(abs(p.shares), share_precision)
        if qty <= 0 or not p.price or p.price <= 0:
            results.append(SubmitResult(p.ticker, p.action, qty, 0.0,
                                        "SKIPPED (zero qty / no price)"))
            continue
        lmt = marketable_limit(p.action, p.price, slippage_cap)
        try:
            r = await conn.place_limit_order(p.ticker, p.action, qty, lmt)
            results.append(SubmitResult(p.ticker, p.action, qty, lmt,
                                        r.status, r.order_id))
        except Exception as exc:               # pragma: no cover - defensive
            results.append(SubmitResult(p.ticker, p.action, qty, lmt, f"FAILED: {exc}"))
    return results
