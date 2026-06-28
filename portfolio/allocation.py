"""Pure rebalance engine — target weights + holdings → a trade plan.

No IBKR, no DB, no network: `compute_plan(...)` takes plain dicts/lists and
returns a `RebalancePlan`.  This is the heart of the new system and is designed
to be unit-tested in isolation (the execution wrapper that fetches live IBKR
state and submits orders is a separate, thin module — Phase 2).

Sleeves
-------
- **core** / **satellite_qv** — the *managed book*.  Their weights are rebalance
  *targets*; the engine trims/tops them back to target, but only when a sleeve
  has drifted more than `band` (avoids churning for small drift).  Idle cash sits
  in the managed book and is deployed into underweight sleeves by the same logic.
- **satellite_bigbet** — capped venture convexity.  **Excluded from drift
  rebalancing entirely** (see docs/strategy/risk_premia_harvesting.md §4):
    * held  → HOLD forever (never trimmed on the upside, never topped up on the
      downside — trimming a winner would cap the 10–100× tail the bucket exists
      for; topping up a loser is averaging down into a breaking thesis);
    * unheld target → a one-time **entry** BUY to its cap (`weight × nlv`);
    * a dead big-bet is handled by *removing its target* (then its residual, if
      any, shows up as an untracked holding) — the freed cap is filled by a new
      conviction name on judgment, not by the rebalancer.
  The big-bet capital is earmarked out of the managed book so the core/qv
  rebalance never tries to spend it.

Conventions: fractional shares; a `cash_buffer` fraction of NLV is held back
(proportionally across the managed sleeves); dollars are signed (+buy / −sell).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

CORE = "core"
SAT_QV = "satellite_qv"
SAT_BIGBET = "satellite_bigbet"
MANAGED = (CORE, SAT_QV)

# A big-bet (or any holding) worth less than this fraction of NLV is "≈ dead".
_DEAD_FRAC = 0.005


@dataclass(frozen=True)
class Target:
    ticker: str
    sleeve: str          # CORE | SAT_QV | SAT_BIGBET
    weight: float        # managed: rebalance weight (of NLV); big-bet: entry cap
    label: str = ""


@dataclass
class TradeProposal:
    ticker: str
    sleeve: str
    current_value: float
    target_value: float
    current_wt: float    # fraction of NLV (display)
    target_wt: float     # fraction of NLV (display)
    drift_pp: float      # (current − target) as % of the *managed* book (managed sleeves only)
    action: str          # BUY | SELL | HOLD
    dollars: float       # signed: + buy, − sell
    shares: float        # fractional, signed
    price: float
    reason: str


@dataclass
class RebalancePlan:
    as_of: datetime
    nlv: float
    cash: float
    managed_nlv: float          # NLV minus big-bet capital (the rebalanceable book)
    bigbet_value: float         # held big-bets, at market
    bigbet_pending: float       # capital earmarked for unheld big-bet entries
    band: float
    proposals: list[TradeProposal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def trades(self) -> list[TradeProposal]:
        return [p for p in self.proposals if p.action != "HOLD"]

    @property
    def turnover_pct(self) -> float:
        return (sum(abs(p.dollars) for p in self.trades) / self.nlv * 100.0) if self.nlv else 0.0


def compute_plan(
    targets: list[Target],
    holdings: dict[str, float],          # ticker → shares held
    prices: dict[str, float],            # ticker → price
    nlv: float,
    cash: float,
    *,
    band: float = 0.05,
    cash_buffer: float = 0.01,
    as_of: datetime | None = None,
) -> RebalancePlan:
    """Return a reviewable rebalance plan.  Pure: no side effects."""
    as_of = as_of or datetime.now(timezone.utc).replace(tzinfo=None)
    by_ticker = {t.ticker: t for t in targets}
    managed = [t for t in targets if t.sleeve in MANAGED]
    bigbets = [t for t in targets if t.sleeve == SAT_BIGBET]

    def value(ticker: str) -> float:
        return holdings.get(ticker, 0.0) * prices.get(ticker, 0.0)

    # ── Big-bet earmarks: held value + pending entries (unheld targets) ──────────
    bigbet_value = sum(value(t.ticker) for t in bigbets)
    bigbet_pending = 0.0
    for t in bigbets:
        if value(t.ticker) <= _DEAD_FRAC * nlv:        # not (meaningfully) held → pending entry
            bigbet_pending += t.weight * nlv

    managed_nlv = nlv - bigbet_value - bigbet_pending
    managed_base = max(managed_nlv - cash_buffer * nlv, 0.0)
    sum_managed_wt = sum(t.weight for t in managed) or 1.0
    # Stated weights are fractions of NLV.  If they fit the managed base, use them
    # *raw* — any unallocated remainder (e.g. an as-yet-unfilled satellite slot, or
    # the cash buffer) deliberately stays in cash rather than inflating the core.
    # Only if a ballooning big-bet has shrunk the managed base below the stated
    # total do we scale the managed sleeves down proportionally to fit.
    fits = (sum_managed_wt * nlv) <= managed_base

    proposals: list[TradeProposal] = []
    notes: list[str] = []

    # ── 1) Managed sleeves: rebalance to normalized target within the managed book
    for t in managed:
        price = prices.get(t.ticker)
        cur_val = value(t.ticker)
        tgt_val = t.weight * nlv if fits else (t.weight / sum_managed_wt) * managed_base
        drift_val = tgt_val - cur_val                  # + buy, − sell
        drift_frac = (cur_val - tgt_val) / managed_nlv if managed_nlv else 0.0
        cur_wt = cur_val / nlv if nlv else 0.0
        tgt_wt = tgt_val / nlv if nlv else 0.0

        if price is None or price <= 0:
            proposals.append(TradeProposal(
                t.ticker, t.sleeve, cur_val, tgt_val, cur_wt, tgt_wt, drift_frac * 100,
                "HOLD", 0.0, 0.0, price or 0.0, "no price — skipped"))
            notes.append(f"no price for {t.ticker}; skipped")
            continue

        if abs(drift_frac) <= band:
            action, dollars, shares = "HOLD", 0.0, 0.0
            reason = f"within band ({drift_frac * 100:+.1f}pp)"
        else:
            action = "BUY" if drift_val > 0 else "SELL"
            dollars, shares = drift_val, drift_val / price
            reason = f"drift {drift_frac * 100:+.1f}pp exceeds band ±{band * 100:.0f}pp"
        proposals.append(TradeProposal(
            t.ticker, t.sleeve, cur_val, tgt_val, cur_wt, tgt_wt, drift_frac * 100,
            action, dollars, shares, price, reason))

    # ── 2) Big-bets: never drift-rebalanced; held → HOLD, unheld → one-time entry
    for t in bigbets:
        price = prices.get(t.ticker)
        cur_val = value(t.ticker)
        cur_wt = cur_val / nlv if nlv else 0.0
        if cur_val > _DEAD_FRAC * nlv:                 # held → leave it alone
            proposals.append(TradeProposal(
                t.ticker, t.sleeve, cur_val, cur_val, cur_wt, t.weight,
                0.0, "HOLD", 0.0, 0.0, price or 0.0,
                f"big-bet — floats free, never trimmed ({cur_wt * 100:.1f}% of NLV)"))
        elif price and price > 0:                      # unheld target → entry buy to cap
            entry_dollars = t.weight * nlv
            proposals.append(TradeProposal(
                t.ticker, t.sleeve, cur_val, entry_dollars, cur_wt, t.weight,
                0.0, "BUY", entry_dollars, entry_dollars / price, price,
                f"big-bet entry (initiation) to cap {t.weight * 100:.1f}%"))
            notes.append(f"initiating big-bet {t.ticker} at cap {t.weight * 100:.1f}% "
                         f"(${entry_dollars:,.0f}); sized so a total loss is acceptable")
        else:
            proposals.append(TradeProposal(
                t.ticker, t.sleeve, cur_val, 0.0, cur_wt, t.weight,
                0.0, "HOLD", 0.0, 0.0, 0.0, "big-bet target but no price — skipped"))

    # ── 3) Untracked holdings (held but not in any target) → flag for sell-to-0
    for ticker, shares_held in holdings.items():
        if ticker in by_ticker or not shares_held:
            continue
        price = prices.get(ticker, 0.0)
        cur_val = shares_held * price
        proposals.append(TradeProposal(
            ticker, "untracked", cur_val, 0.0, cur_val / nlv if nlv else 0.0, 0.0,
            0.0, "SELL" if price > 0 else "HOLD", -cur_val,
            -shares_held if price > 0 else 0.0, price,
            "untracked holding — not in target_allocation"))
        notes.append(f"untracked holding {ticker} ({shares_held:g} sh) — not in targets")

    return RebalancePlan(
        as_of=as_of, nlv=nlv, cash=cash, managed_nlv=managed_nlv,
        bigbet_value=bigbet_value, bigbet_pending=bigbet_pending, band=band,
        proposals=proposals, notes=notes)
