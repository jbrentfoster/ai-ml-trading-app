"""Portfolio construction & rebalancing (the new system's core).

The risk-premia harvesting book: an 80% diversified value+quality ETF core, a
~15% quality-value stock satellite, and a ~5% conviction "big-bet" sub-bucket.
See docs/strategy/risk_premia_harvesting.md.

Modules
-------
allocation  — PURE rebalance engine: target weights + holdings -> a trade plan.
              No IBKR, no DB, no network — unit-testable in isolation.
              (rebalancer.py — execution wrapper — and holdings.py are later phases.)
"""

from portfolio.allocation import (
    CORE,
    SAT_BIGBET,
    SAT_QV,
    RebalancePlan,
    Target,
    TradeProposal,
    compute_plan,
)

__all__ = [
    "CORE",
    "SAT_QV",
    "SAT_BIGBET",
    "Target",
    "TradeProposal",
    "RebalancePlan",
    "compute_plan",
]
