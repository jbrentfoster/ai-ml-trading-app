# Risk-Premia Harvesting — Strategy & Refactor Plan

**Status:** adopted (2026-06-27) — core direction validated (see §2.1, the value+quality 63-year backtest passed Phase-0 net of costs). Implementation: **A-now / B-as-lab** (see §4).
**Supersedes:** the predictive-alpha approach (ML ensemble + LLM news). See [`pivot_decision_2026-06.md`](pivot_decision_2026-06.md) for *why*.

## 0. Scope & context (who this is for)

This is a **learning / science project**, not the operator's primary wealth vehicle. Stated explicitly so design choices stay honest:

- The operator's wealth is largely in a **401k (index funds)** + a **professionally-managed account**. This project is a small, separate sleeve — likely **$10–20k** if it ever goes live.
- Consequences that *shape the design*:
  - **The value drought is not a financial threat here.** The capital is small and risk-tolerant, and the core wealth is elsewhere — so the operator can actually *harvest the patience premium that institutions and most retail cannot* (they're forced out by career/redemption risk; he isn't). The structural edge is literally his situation. → We **lean into** the tilt rather than watering it down.
  - **This sleeve is a diversifier vs the core** (the 401k is cap-weighted index = mega-cap-growth beta; a value+quality tilt is a *different* exposure). So lagging SPY in a growth bull matters even less.
  - **Simple, robust, well-documented beats clever** — the project doubles as something the operator's **kids could learn from / use someday**. A transparent quality-value-and-patience strategy (and the *method* used to reach it — killing three mirages with cheap probes before committing) is the real inheritance.
- **Redefined success:** understanding + durability + transmissibility, harvested with discipline — *not* beating SPY total return.

---

## 1. The goal — defined as risk-adjusted, not absolute

Risk-premia harvesting **will usually lag 100% SPY in a bull market.** That is not failure — it is the price of diversification and drawdown protection. So success is defined up front in risk-adjusted terms, or the strategy will be misjudged every time equities run.

- **Primary metrics:** Sharpe, max drawdown, Calmar (CAGR / |maxDD|), ulcer index.
- **Benchmark:** a passive **60/40** as the honest comparison, with **SPY shown for reference** so the diversification trade-off is explicit.
- **Win condition:** equity-like returns with **materially smaller drawdowns**, and *survival* through the regimes (2008, 2020, 2022) where 100%-equity halves.

> If the operator's true objective is "beat SPY total return," this strategy does **not** deliver that and we should say so before building. The whole point is a better *risk-adjusted* outcome, not a higher number in a bull market.

---

## 2. The strategy

A blend of three transparent, low-turnover, well-documented ideas — chosen for robustness over cleverness (we have a lot of 2026-06 evidence that clever-and-fragile loses):

1. **Diversified asset-class allocation.** A *fixed* set of ~8–12 ETFs spanning equities (US / international), duration (treasuries), inflation (TIPS / commodities / gold), credit, and real assets. Harvests the equity / term / credit / commodity premia. No stock rotation, no prediction.
2. **Absolute-momentum / trend gating** (Faber GTAA, Antonacci dual-momentum). Hold each asset only while it is in an uptrend (above its ~10-month MA / positive 12-month return); otherwise rotate to cash / T-bills. **This is the trend-following that actually works** — at the *diversified multi-asset* level, where uncorrelated trends smooth the whipsaw. (Contrast: trend-timing a single high-beta cohort failed badly — see [`../findings/volatility_cohort_edge.md`](../findings/volatility_cohort_edge.md) §status-log, "regime-timing and vol-scaling overlays.")
3. **Volatility targeting.** Scale total exposure to a constant portfolio volatility (e.g. 10%/yr), de-levering in turbulence. Portfolio-level discipline, applied to a *diversified* book (where vol-targeting is robust, unlike on one violent instrument).

Plus the unglamorous edges that genuinely belong to a retail operator: **low costs, low turnover, tax-lot management, and tax-loss harvesting.**

### 2.1 The equity sleeve = a value + quality tilt (validated)

The equity portion of the allocation is **tilted to value + quality** rather than cap-weighted — this is the one direction that *passed* a hard backtest, and it matches the AQR decomposition of Buffett's record (quality + value + low-beta + patience, minus the leverage retail can't get; Frazzini-Kabiller-Pedersen, "Buffett's Alpha," 2018).

Evidence (Fama-French 5-factor, 1963–2026, point-in-time correct):

| | CAGR | Sharpe | maxDD |
|---|---|---|---|
| Market | 10.83% | 0.46 | −50% |
| Value+Quality tilt, **net of ~0.5%/yr friction** | 13.32% | **0.63** | −51% |

**+2.16%/yr net active**, higher Sharpe, robust to costs; the tilt beat the market in **83%** of rolling 5-year windows and was nearly *flat* (+4.9%) while the market fell −38% in the 2000–02 dot-com bust (the value anchor at work). Real tradeable ETFs (VLUE+QUAL, net of fees) over **2014–2026 — value's worst era in history** — still **matched SPY** (13.1% vs 13.6% CAGR, Sharpe 0.79 vs 0.83), lagging only ~0.5%/yr with a −24.8% worst relative point.

**The toll booth (state it plainly so future-self doesn't capitulate):** value underperformed for ~13 years (2007–2020); pure HML fell −56%; the tilt lagged the market by 15.5% over that stretch. The premium *exists because* most can't endure that. Given §0 (small, drought-tolerant capital), the operator can — which is the whole point.

**Full-cycle ETF reality check (2000–2026) — read this before believing the 63-year number.** The FF figure is the *academic* premium (long-short, deep-value, quality-screened) and is heavily *pre-2000*. In real ETFs the picture is humbler and path-dependent: plain value (IWD, Russell 1000 Value) **beat SPY for ~20 years** (up to +50% ahead by 2007, net-ahead through ~2020), then the historic 2021–2025 mega-cap/AI growth concentration erased the lead, leaving IWD net **−7.4% vs SPY over the full 26 years** (8.21 vs 8.53%/yr, *worse* maxDD). Two takeaways: **(1) quality matters** — QUAL > VLUE > IWD; value-alone owns the traps, so use value *and* quality, never value alone. **(2) Calibrate the forward expectation to ~*match* SPY with crash-protection and a diversifying exposure — NOT to beat it.** The repeatable part is bubble-protection (IWD −13.7% vs SPY −33.7% in 2000–02) plus a bet that today's record growth concentration eventually normalizes (when it has, value snapped back hard). For §0's small, drought-tolerant, learning sleeve that is an appropriate bet; it is **not** a reliable SPY-beater, and the doc should never imply it is.

---

## 3. Keep / scrap / build — mapped to the codebase

| Layer | Action | Notes |
|---|---|---|
| Execution + reconciliation (`execution/ibkr_connection.py`, `execution/reconciliation.py`, `data/flex_client.py`, `fill_log`, Phase B) | **KEEP — crown jewels** | The hard, working part. Allocation still needs robust placement + fill reconciliation. |
| Data pipeline (`data/fetcher.py`, `data/database.py`) | **KEEP, simplify** | Fetch OHLCV for a *fixed* ETF universe; compute MAs / vol / momentum (far simpler than the 17-feature set). |
| Dashboard (Streamlit) | **KEEP, repurpose** | Swap model/signal pages for allocation, risk-budget, drawdown, tax pages. |
| Trade / benchmark logging (`trade_log`, benchmark tracking) | **KEEP** | Still need realized P&L + benchmark-relative tracking. |
| Circuit breaker (`risk/circuit_breaker.py`) | **KEEP** | Repurpose as a portfolio-level drawdown halt. |
| ML ensemble (`models/lstm`, `xgboost`, `finbert`, `ensemble`, `signal_gate`, `walk_forward`, `regime_detector`, `trade_patterns`) | **SCRAP** | The retired predictive-alpha engine. |
| LLM news analyst (`models/llm_analyst.py`, `data/news_dedup.py`, ingest/score scripts) | **SCRAP** | Retired (compute + data-source constraints). |
| Universe selection (`data/universe.py`, 3-stage rotation) | **REPLACE** | Fixed asset-class ETF list (+ optional factor ETFs). No rotation. |
| Position sizer (`risk/position_sizer.py`, Kelly-on-signal) | **REPLACE** | New sizing = target weights × vol-target scalar, not Kelly on a prediction. |
| Risk guards (sector / correlation / duplicate), brackets, trailing stops | **MOSTLY SCRAP** | Stock-picking machinery. Allocation de-risks via trend gates + vol targeting, not per-position stops. |
| **NEW: allocation engine** | **BUILD** | Target weights from the strategy rules. |
| **NEW: portfolio backtester** | **BUILD** | Vectorized multi-asset backtest (can lean on `data/walk_forward.py`, model-agnostic). |
| **NEW: rebalancing engine** | **BUILD** | Band-based, low-turnover; target weights → orders via the kept execution stack. |
| **NEW: tax-lot accounting + loss harvesting** | **BUILD (later)** | The genuine retail edge; needs lot-level accounting. |

The shape of the refactor: **strip the prediction machinery, keep the plumbing, build an allocation + risk-overlay engine on top.**

---

## 4. Implementation: 80/20 core-satellite (confirmed 2026-06-27)

A **lighter, equity-tilted** posture (option (ii)) — more equity, gentler de-risking, accepting deeper drawdowns the operator can tolerate per §0 — split into a disciplined core and a concentrated learning satellite:

- **80% — ETF core (A).** Diversified base + value+quality equity tilt via ETFs (VLUE/QUAL-type) with a *light* trend/vol-target overlay. Robust, low-cost, transmissible — the disciplined foundation. **This is the bulk of what trades.**
- **20% — satellite (B), split into two sub-buckets:**
  - **~15% quality-value** — ~3–5 concentrated **large-cap** names surfaced by `scripts/buffett_screen.py` (rank-percentile composite `0.45·quality + 0.30·safety + 0.25·value`, AQR "Buffett's Alpha" criteria) + operator judgment. A *screen feeding real (small) money*, not a predictor. For upside optionality + active learning, **NOT modeled as reliable alpha** (same value+quality premium as the core, higher variance).
  - **~5% conviction "big bets"** (≤ ¼ of the satellite, **≤ 2–3% per name, 1–2 names**) — high-conviction *venture/growth* positions (e.g. a frontier-AI-lab IPO) that **deliberately do NOT fit the value/quality screen**. This is *capped venture convexity, not value investing*: no margin of safety on valuation; the bet is the 10–100× tail (think GOOG/AMZN/AAPL bought at IPO), with downside bounded by the cap. Shares the buy-and-hold *horizon* of the rest of the satellite but **not** its selection discipline — a separate, clearly-labelled strategy.

  **Big-bet disciplines (what separates an asymmetric bet from gambling):** (1) **size so a total loss is genuinely fine** — if losing it all would sting, it's too big (non-negotiable); (2) **write the thesis** incl. the venture caveat ("I'll probably lose most of it, sized so that's OK; the case is the tail") and what would *invalidate* it — the company losing its competitive position, **not** a price drop; (3) **hold through brutal volatility** (every GOOG/AMZN/NVDA had 50–80% drawdowns en route) — sell only on a *broken thesis*, never on a drawdown, and **no averaging down** into a breaking thesis. The asymmetry that justifies it: at ≤5% total, a wipeout of *all* big bets costs ~5% of the portfolio (a shrug), while a 20× on a 2.5% position adds ~50% (transformational) — the cap is what makes that safe rather than reckless. (On the operator's specific interest, a frontier-AI-lab IPO: treat it as venture, not a sure thing — capital-intensive, cash-burning, intensely competitive, likely a rich IPO valuation; size accordingly.)

  **Big-bets are exempt from rebalancing — the cap is an *entry* limit, not a rebalance *target*.** Unlike the core and the quality-value satellite (whose weights are *targets* the rebalancer trims/tops back to), a big-bet's cap governs only how much capital you *deploy at initiation* (validated against cost). After that it **floats free**: (a) **upside — never trimmed on drift** — the allocation engine excludes big-bets from the band/drift logic entirely; trimming a winner back to 2–3% would cap the 10–100× tail the bucket exists to capture (i.e. selling Amazon at $100 to stay diversified); (b) **downside — replaced, not topped up** — a cratered/delisted name is *not* averaged down; it frees room in the ≤5% bucket for a *new* big-bet on fresh conviction (a deliberate judgment call, not an auto-rebalance). The engine's only big-bet jobs: enforce caps **at entry**, and surface "room available" when one dies.

  **Windfall threshold (a deliberate decision, NOT a rebalance rule).** Because winners aren't trimmed, a bet that 40×'s can become a huge share of the portfolio — the *good* problem, but real single-name concentration (these names can still round-trip 80% *after* they've made you). Pre-decide a *high* threshold (e.g. once a big-bet exceeds ~25–30% of the whole portfolio) at which to take *some* off — not to cap potential, but to lock a life-changing gain you'd hate to give back. Default lean for this learning/legacy sleeve: let it run, but set the threshold **in advance** so the call isn't made emotionally at the peak.

**Why (ii) and not the full all-weather version — the full-strategy backtest (2006–2026):** the *fully* defensive build (diversified + trend-gate-to-cash + vol-target) delivered a −8% max drawdown and **+1.5% in 2008** at Sharpe 0.86 (≈60/40, > SPY), but only ~5–6% CAGR — *too* defensive for §0's small, drought-tolerant capital (it trades ~6%/yr of return for protection this sleeve doesn't need). So the core runs a **lighter** overlay: heavier value-tilted equity, gentler de-risking, equity-like returns with deeper (tolerable) drawdowns.

**Satellite caveats (baked in, enforced by the screen's own header):** diversify the *quality-value* sub-bucket across 3–5 names (never 1–2 mega-bets there) — the *big-bet* sub-bucket is the one deliberate, hard-capped exception; lean on the **quality** filter to dodge value traps; the screen is a *shortlist, not a buy list* — moat durability + management quality (the most valuable part) can't be screened and are where the *learning* lives; financials/utilities/REITs mis-score on the safety axis and need separate judgment; can't be backtested (point-in-time fundamentals lookahead), so the satellite's selection is proven only by **paper-trading forward** — the premium it harvests is already validated (§2.1).

## 4b. Phased roadmap (validate-before-build)

- **Phase 0 — Backtest *before any refactor*.** ✅ **Done & passed** — the value+quality tilt (§2.1) cleared the gate net of costs across 63 years and held up in real ETFs through value's worst era. (Same cheap-probe discipline that retired three alpha directions in 2026-06.)
- **Phase 1 — Allocation engine + dashboard** producing live target weights (read-only, no orders).
- **Phase 2 — Rebalancing → execution**, wired to the existing IBKR / reconciliation stack; paper-traded.
- **Phase 3 — Tax-lot accounting + loss harvesting + cost discipline** (the retail edge).
- **Phase 4 — Decommission** the scrapped ML / news modules to an archive branch (preserve the learning history; do not delete).

---

## 5. Known limitations (be honest)

- Risk-parity / duration-heavy allocations **suffer in rising-rate regimes** (cf. 2022, where bonds and equities fell together).
- Trend gating **whipsaws in choppy, directionless markets** and lags V-recoveries (mitigated, not eliminated, by multi-asset diversification).
- Factor premia (value/momentum/quality) **decay post-publication** (McLean-Pontiff) and are crowded — treat tilts as modest, not a free lunch.
- This is **alternative beta, not alpha.** The honest claim is better risk-adjusted return and drawdown survival, not market-beating returns.

---

## 6. Open decisions (for the operator)

1. ~~Confirm the success definition~~ — **resolved (§0):** understanding + durability + transmissibility on small, drought-tolerant capital; risk-adjusted vs 60/40, not raw vs SPY.
2. ~~MVP ambition~~ — **resolved (§4):** simple ETF core (A) now; custom screen (B) as a paper learning lab. Value+quality tilt *included* (validated), not deferred.
3. ~~Universe~~ — **PINNED (2026-06-27).** Starting strategic allocation (of total 100%), equity-tilted per (ii), simple + transmissible:

   | Sleeve | Ticker | Weight | Role |
   |---|---|---|---|
   | US value | **VLUE** | 22% | value factor |
   | US quality | **QUAL** | 22% | quality factor |
   | Intl developed value | **EFV** | 8% | diversification + value |
   | US Treasuries 7–10y | **IEF** | 14% | duration / deflation hedge |
   | Gold | **GLD** | 8% | crisis / inflation hedge (IAU = cheaper alt) |
   | Broad commodities | **PDBC** | 6% | inflation hedge (no K-1, unlike DBC) |
   | **Core subtotal** | | **80%** | |
   | Satellite: quality-value | 3–5 large-caps | 15% | `scripts/buffett_screen.py` + judgment |
   | Satellite: conviction big-bets | 1–2 names | 5% | venture convexity, off-screen, ≤2–3%/name (§4) |

   Net exposure ≈ **72% equity / 28% diversifiers** (52% core equity ETFs + 20% satellite). Adjustable; this is the v1 starting point, not dogma.
4. ~~Rebalance cadence~~ — **PINNED.** **Quarterly**, **band-based** (only trade a sleeve that has drifted > ~5 absolute pts from target) to keep turnover/cost/tax low. The trend-gate / vol-target overlay from §2 is **deferred to v2** — v1 runs the static strategic allocation + band rebalance only (the full-strategy backtest showed the heavy overlay is too defensive for this scope; add a *light* version later only if wanted).
