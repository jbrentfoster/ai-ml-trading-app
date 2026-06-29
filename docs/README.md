# Documentation index

Docs for the **risk-premia harvesting** system (the project pivoted from
predictive-alpha in 2026-06 — the old technical tutorials are archived under
[`../archive/docs/`](../archive/docs/README.md)).

## Start here

| Doc | What it is |
|---|---|
| [`strategy/risk_premia_harvesting.md`](strategy/risk_premia_harvesting.md) | **The strategy + plan.** 80% value+quality ETF core / 20% Buffett-style satellite (quality-value + capped conviction big-bets); how returns are harvested; the honest limitations. |
| [`strategy/pivot_decision_2026-06.md`](strategy/pivot_decision_2026-06.md) | **Why we pivoted.** The decision record: four predictive-alpha directions tested with cheap probes and retired on evidence. |
| [`operating_guide.md`](operating_guide.md) | **How you actually run it.** The initial / quarterly / periodic workflows, the two-gate execution, reading the Allocation dashboard. |
| `../CLAUDE.md` | The build/architecture reference (for working *on* the code). |

## The science record (the "why")

The evidence trail behind the strategy — kept in place as the project's lab
notebook (this is a learning project; the *reasoning* is the point).

- [`findings/`](findings/README.md) — distributional diagnostic observations across
  many trades (incl. the pivot evidence: `volatility_cohort_edge.md`,
  `news_attribution_misallocation.md`).
- [`case_studies/`](case_studies/) — individual trade post-mortems, incl. the
  `losers_*` studies that fed the pivot.
- [`review-system.md`](review-system.md) — how observations turn into findings /
  case studies / enhancements / follow-ups (the review discipline; reflects the
  old daily-trade cadence but the artifact taxonomy still applies).
- [`enhancements.md`](enhancements.md) · [`reviews/followups.md`](reviews/followups.md)
  — open/forward-looking work and per-run follow-up gates.

## Retired

- [`../archive/docs/`](../archive/docs/README.md) — the predictive-alpha technical
  tutorials (LSTM/XGBoost/FinBERT/ensemble/universe/LLM). Archived, not deleted;
  full v1 codebase at git tag `v1.0-predictive-alpha`.
