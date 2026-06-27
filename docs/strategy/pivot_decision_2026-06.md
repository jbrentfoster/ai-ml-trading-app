# Pivot Decision — From Predictive Alpha to Risk-Premia Harvesting

**Date:** 2026-06-27
**Status:** adopted. Predictive-alpha directions retired on evidence (below); risk-premia harvesting **validated** and adopted (the value+quality tilt cleared a 63-year costed backtest — see Resolution + [`risk_premia_harvesting.md`](risk_premia_harvesting.md) §2.1).

---

## Context

The paper account was ~flat after 2.5 months (realized +$8,016 over 62 live round trips). A multi-analysis investigation (2026-06-26/27) tested whether the system had — or could be redesigned to have — durable predictive alpha. Three directions were probed cheaply and **all three failed**, each confirmed from multiple angles. The cheap-probe-first discipline retired each in hours/days rather than months.

## What was retired, and the evidence

1. **Price momentum / volatility-cohort signal.** Real *in-sample* monthly-horizon selection (beta-independent), but it **inverts in bear regimes** — a known, crowded, pro-cyclical risk premium with crash risk, not durable alpha. Regime-timing and vol-scaling overlays failed to rescue it (flip = −66% DD; vol-scaling worse than static). Full record: [`../findings/volatility_cohort_edge.md`](../findings/volatility_cohort_edge.md) (Observation §§1–7 + status log). Scripts: `scripts/analyze_volatility_cohorts.py`, `analyze_score_forward_returns.py`, `analyze_score_beta_decomposition.py`, `analyze_premise_regime.py`.

2. **LLM news re-attribution.** The differentiated idea (resolve feed-mistagged articles to the company they're actually about). Two killers: (a) **structurally untestable** in the current pipeline — resolved tickers are mega-caps (median $272B), because the universe and resolver are both big-cap-centric; (b) the live forward-return preview was **faint even after excess-vs-SPY adjustment**. Retired by the operator on the practical constraints: it would need far more compute to process news at scale and many more news sources than a laptop + IBKR feed can provide. Related finding: [`../findings/news_attribution_misallocation.md`](../findings/news_attribution_misallocation.md).

3. **News-driven drift in under-covered small-caps** (Hong-Lim-Stein "slow information diffusion"). The intended pond if (1)/(2) were dead. **Premise not supported:** two cheap offline probes (generic catalyst, then a clean overnight-gap catalyst) found **no post-catalyst drift** in low-coverage names — ~0 drift, ~50% continuation, no coverage gradient. The earlier apparent "reversal" was microstructure (it vanished under the gap proxy). The pond is efficient. (Probe scripts in scratchpad; not promoted — dead-end tests.)

## Rationale

The pattern is the empirical version of the market-structure reality: **durable predictive alpha from public price/news data, on a laptop, kept not being there** — confirmed from new angles each time. This is consistent with "efficiently inefficient" markets (Grossman-Stiglitz): edge is payment for a risk borne, a service provided, or information/skill held — and a retail operator on commodity public data has none of the resource advantages (speed, alt-data, scale, execution) that fund the real edges.

The realistic retail win is **not** a magic signal. It is the unglamorous stack: harvest known risk premia cheaply, control costs/taxes/turnover, manage risk ruthlessly, and survive drawdowns. The project's most valuable asset — the execution/reconciliation/risk plumbing — is **already built for that** and is kept.

## Decision

Pivot to **disciplined risk-premia harvesting + ruthless cost/tax/risk control + drawdown survival**, measured on risk-adjusted metrics vs a 60/40 benchmark (not raw return vs SPY). Strip the prediction machinery, keep the plumbing, build an allocation + risk-overlay engine. Full plan: [`risk_premia_harvesting.md`](risk_premia_harvesting.md).

The scrapped ML / news modules are to be **archived, not deleted** — they are the learning history that produced this decision.

---

## Resolution (2026-06-27)

After the decision, two further alpha ideas were explored and **also retired on cheap evidence**, and the pivot was then **validated**:

- **News-sentiment sector rotation** (overweight/underweight sector ETFs by aggregated headline sentiment): tested on 58k existing FinBERT-scored headlines → 766 sector-day observations. Cross-sectional rotation on sentiment *level* and *change* both ~0 (t < 1.6); within-sector quintiles non-monotonic noise. **Null.** The aggregation-denoising bet (the thing that made it better than stock-level news) did not pay off.
- **Contrarian / "be greedy when others are fearful"**: a market-level flicker appeared (fear quintile → higher forward SPY return), but the decades-long price-proxy test (^GSPC 1960–2026) showed it is just weak short-term mean-reversion (~+0.3%/10d), and the *extreme* "buy the crash" version **failed in the worst crises** (2008 −0.6%, 2020 −2.4% at 10 days — falling knife). Buffett's edge is value-anchored + multi-year, not short-horizon sentiment timing. **Closed.**

- **Value + quality — VALIDATED.** The one direction that passed (Fama-French 1963–2026, point-in-time correct): the tilt beat the market by **+2.16%/yr net of friction** at higher Sharpe (0.63 vs 0.46), won 83% of rolling 5-yr windows, and was near-flat in the 2000–02 bust while the market fell 38%. Real ETFs (VLUE+QUAL) matched SPY even through value's worst era (2014–2026). The cost is a survivable temperament toll (the 2007–2020 drought). Detail: [`risk_premia_harvesting.md`](risk_premia_harvesting.md) §2.1.

**Scope clarified by the operator:** this is a learning/science project on a small ($10–20k), drought-tolerant sleeve, with core wealth elsewhere (index 401k + managed account) — possibly a legacy tool for his kids. That scope *enables* the strategy: he can harvest the patience premium institutions and most retail can't. Success = understanding + durability + transmissibility, not beating SPY. Implementation: **A-now / B-as-lab** (ETF core deployed; custom screen as a paper learning lab) — [`risk_premia_harvesting.md`](risk_premia_harvesting.md) §4.

**The alpha question is now closed for this project.** Four predictive-alpha directions tested and retired; the durable path is premium-harvesting + patience.
