# Trail vs TP capture — the exit-mechanism choice is a path-shape bet, ~net-neutral at current n

## Status

**observed** (2026-06-16) · **hypothesized** (2026-06-16) · untested

Sister to `tp_concentration.md` (that finding asks *which exit reason carries the alpha*; this one asks *which exit mechanism captures more on the fast movers*, given that bracket-TP and trailing-stop are mutually-exclusive exits for the same winning setup). Motivated by the WDC 2026-06-16 case study (`docs/case_studies/wdc_2026-06.md`) and the matched AXTI↔SNOW pair (`axti_2026-04.md`, `snow_2026-04.md`). Directly bears on the CLAUDE.md enhancement "Trailing stop is structurally crowded out by the bracket TP on fast moves".

---

## Observation

The CLAUDE.md "crowded out" entry (n=1, AXTI) framed the trail as *losing upside to the TP on fast moves*. With the live book now at n=5 TP exits and n=6 trailing exits, the measurement shows the relationship is **two-sided and path-dependent**, not one-directional.

Two symmetric counterfactuals were computed for every `source='live'` winner against `ohlcv_bars` + `indicator_snapshots` (harness: `scripts/analyze_trail_vs_tp.py`, post-window = 10 trading bars, trail = 2.0×ATR):

**TP exits — what a continued 2×ATR trail (started at the TP exit price) would have ADDED over the next 10 bars:**

| Symbol | realized % | trail-add % | trail-add $ | window |
|---|---|---|---|---|
| AXTI | +40.1 | **+8.7** | +4,797 | complete |
| MRVL | +24.8 | **+17.1** | +8,399 | complete |
| AAPL | +1.7 | +3.2 | +52 | incomplete (trail not fired in 10 bars) |
| SPY | +2.4 | **−1.3** | −516 | complete (TP beat trail) |
| WDC | +21.5 | — | — | no post-exit bars yet (exited 6/16) |

Across the 4 measurable TP exits: **mean trail-add +6.9%, total +$12,732, positive in 3/4.** The fixed TP forfeited real upside on the two big fast movers (AXTI +8.7%, MRVL +17.1%) and was *correct* on the slow one (SPY −1.3%).

**Trailing exits — did the trail beat the fixed TP it replaced?** (the bracket TP level was cancelled at Phase-3.5 conversion; this is the counterfactual where we never converted):

| Symbol | realized % (trail) | fixed-TP % | TP touched in hold? | trail beat TP? |
|---|---|---|---|---|
| ASTS | +23.9 | +29.1 | yes | **no (−5.2)** |
| SNOW | +18.2 | +16.8 | yes | yes (+1.4) |
| QQQ | +1.7 | +3.7 | yes | **no (−2.0)** |
| GLW | +6.0 | +19.5 | yes | **no (−13.5)** |
| SNDK | +25.0 | +23.7 | yes | yes (+1.3) |
| AON | +0.8 | +0.8 | no | neutral |

The trail beat the fixed TP in only **2 of 6** conversions (SNOW, SNDK — by ~+1.3–1.4%), and *lost* in 3 of 6, badly in **GLW (+6.0% realized vs +19.5% a fixed TP would have locked)** — GLW peaked at $208.57 (above its $206.84 TP) then the 2×ATR trail rode it back down to a $183.40 exit.

**The mechanism is a path-shape bet:**
- **Gap / continued-run past TP** (no 2×ATR pullback in the following bars) → TP forfeits upside, trail wins: AXTI, MRVL.
- **Spike through TP then reverse ≥2×ATR before the trail ratchets up** → trail gives back, TP wins: GLW, ASTS, QQQ.
- **Steady grind past TP, trail exit lands above the TP level** → trail wins, narrowly: SNOW, SNDK.

Rough net across the current book: TP→trail would have *added* ~+$12.7k on the TP exits, but the conversions that already happened *lost* ~−$7.2k vs the TP they replaced (≈ −$5.4k of that is GLW alone). Net is ambiguous and dominated by a handful of trades.

---

## Sample / dataset

- **Source**: `trade_log` `source='live'` on `db/trading.db` as of 2026-06-16 (55 live rows total). Winners only: 5 `tp`, 6 `trailing`, 10 `signal_flip` (signal_flip shown in the harness output for context but not central here).
- **Entry ATR**: nearest `indicator_snapshots.atr_14` at/before entry date. **Fixed-TP level**: `order_decisions.take_profit_price` from the most recent APPROVED BUY before exit (verified correct-cycle for GLW/ASTS/QQQ/SNOW on 2026-06-16 — TP levels and hold-peak highs cross-checked by hand).
- **Counterfactual model**: continue-holding from the realized exit with a `peak − 2×ATR` trail; exit when a subsequent bar's low crosses the stop; if it never fires in 10 bars, mark the window incomplete (still holding at window end — an *upper* bound on trail-add, not a realized number).
- **Known incompleteness**: WDC has 0 post-exit bars (exited the analysis day); AAPL/GE/NBIS/LMT/SLV windows are incomplete (trail had not fired by the window end). MRVL/SPY/AXTI windows are complete.
- **n is small and overlaps the case-study set** — this is the floor of "distributional," not a mature sample. Two of the three big TP-side numbers (AXTI, MRVL) are overnight gap-throughs, i.e. **magnitude is timing luck** (both case studies say so explicitly), so the +$12.7k TP-side figure is partly unrepeatable.

---

## Hypotheses (ranked)

**H1 — Neither mechanism dominates; the outcome is governed by post-activation path shape, which is not predictable ex ante.**
Continued-run favors the trail; spike-and-reverse favors the TP; steady-grind is a near-tie. The book splits roughly evenly across these shapes, so over a large sample the trail and the TP net to approximately the same expected capture, with the trail carrying *higher variance* (it gives back on reverses but rides gaps). Under H1 the correct action is **no change** — keep both mechanisms; the current activation+TP geometry is an unbiased coin-flip on path shape.

**H2 — A feature predicts the path shape, enabling a conditional exit rule.**
A specific refinement of H1: some entry/regime feature separates the continued-run names from the spike-reverse names. Candidates: relative ATR (gap-prone high-vol names run; AXTI ≈13.5%/AAPL≈2.2% relative ATR), regime (TRENDING → run; MEAN_REVERTING → reverse), or recent gap behavior. If H2 holds, a rule like "convert to trail only for TRENDING/high-relative-ATR names; keep the fixed TP for MEAN_REVERTING" would beat the blanket policy. Note the **regime tag at WDC/AXTI/MRVL entry was TRENDING; GLW/QQQ were the reversers** — a candidate discriminator worth testing first.

**H3 — The trail distance (2×ATR) is mistuned, not the activation.**
The trail's losses are concentrated where it gives back too much after a spike (GLW −13.5%). A *tighter* trail (1.0–1.5×ATR) would lock more of the spike before the reversal, while a wider activation would avoid converting marginal moves. This is a parameter-tuning hypothesis: the net could be pushed positive by re-tuning `trailing_stop_trail_atr` / `trailing_stop_activation_atr` rather than by choosing between mechanisms.

**H4 — Small-n / survivorship; the net is noise.**
n≈5 each side, dominated by GLW (−$5.4k) and MRVL (+$8.4k); two TP-side wins are overnight-gap timing luck. Remove any one trade and the net sign can flip. Under H4 there is nothing to act on until Phase B accumulates ≥10 each side.

---

## Discriminating tests

**For H1 / H2 — classify each winner by post-activation path shape and test a feature split.** For every `tp` and `trailing` winner, compute max-favorable-excursion and max-adverse-excursion *after* the price first crossed the activation level (entry + 2×ATR). Continued-run = MFE large, MAE small; spike-reverse = both large. Then test whether `regime` (from `signal_log`), relative ATR (`atr_14 / entry_px`), or entry gap behavior separates the two clusters. If a single feature separates them cleanly, H2 is supported and a conditional rule is worth prototyping; if the shapes are scattered across all feature values, H1 holds and no conditional rule will help.

**For H3 — parameter grid on the counterfactual.** Parameterize `scripts/analyze_trail_vs_tp.py` over `TRAIL_MULT ∈ {1.0, 1.5, 2.0, 2.5}` and an activation grid, recompute both the TP-side trail-add and the trailing-side trail-vs-fixed-TP across the whole book, and find the (activation, trail) pair that maximizes *net* capture across both buckets. If a clearly-positive-EV pair exists and is stable to leaving out the single largest contributor (GLW / MRVL), H3 is actionable; if the optimum is a knife-edge that flips when one trade is dropped, it's overfitting (H4).

**For H4 — wait and recompute.** Re-run the harness when the live book reaches ≥10 `tp` and ≥10 `trailing` exits; check whether the net sign and the per-bucket means stabilize or keep swinging with each new trade.

---

## What we are NOT doing yet, and why

- **Not lowering `trailing_stop_activation_atr` (2.0 → 1.0).** It would have caught AXTI/MRVL via the daily path, but it also converts more marginal moves to trails — and the trail *loses* to the fixed TP on the spike-reverse names (GLW/ASTS/QQQ). At current n the two effects roughly offset; lowering activation is not clearly positive EV.
- **Not flipping `intraday_trail_conversion_enabled` / dropping `intraday_conversion_buffer_atr`.** WDC already proved the buffer would have blocked its 6/15 conversion anyway, and more aggressive intraday conversion would *increase* exposure to the spike-reverse give-back, not just the gap-capture upside. Also reintroduces the mid-session no-stop-window risk the buffer exists to avoid.
- **Not raising `atr_take_profit_multiplier` (3.0 → 5.0).** Would help AXTI/MRVL/WDC ride further, but the TP is also what *correctly* captured SPY and locked GLW/ASTS/QQQ at their TP levels in the counterfactual; widening it surrenders those.
- **Not adding a path-classifier exit rule.** H2 is plausible (TRENDING-vs-reverse split looks promising) but n≈5 per cluster is far too small to fit a rule without a high false-discovery rate — the same n=9 overfitting caution as `tp_concentration.md`.
- **Refines, does not yet act on, the CLAUDE.md "crowded out" entry.** The measurement downgrades "the trail is being crowded out and we're losing upside" to "trail vs TP is a path-shape bet that is ~net-neutral at current n, with the trail carrying higher variance." That refinement is the finding; the parameter decision waits for the H3 grid + more data.

---

## Trigger to revisit / verification gate

Revisit when **any** of the following fires:

- The live book reaches **≥10 `tp` exits AND ≥10 `trailing` exits** — the minimum for the per-bucket means and the net sign to be more than anecdotal (H4 discriminator).
- The **H3 parameter grid** is run and surfaces an (activation, trail-distance) pair with net-positive capture across both buckets that is **stable to leaving out the single largest contributor** (GLW or MRVL). That stability check is the overfitting guard.
- A **3rd fast-move TP-capped winner with a *complete* post-window** lands where a continued trail would have added >10% (AXTI, MRVL are the current two) — strengthens the H2 "continued-run is identifiable" case enough to prototype a conditional rule.
- WDC's post-exit window fills in (≥10 bars after 2026-06-16) — recompute its trail-add; it's currently the only big TP winner with no counterfactual.

---

## Status log

**2026-06-16** — Observed + hypothesized. Surfaced from the WDC +21.5% case study auto-invoked in the 2026-06-16 daily review; the operator asked whether to fix/enhance the trail-activation miss, chose "measure first." Built `scripts/analyze_trail_vs_tp.py` and ran it across the 5 live `tp` + 6 live `trailing` winners. Headline: the relationship is two-sided — TP forfeits upside on gap/continued-run (AXTI +8.7%, MRVL +17.1% a trail would add) but the trail gives back on spike-reverse (GLW +6.0% realized vs +19.5% fixed-TP; ASTS −5.2; QQQ −2.0), beating its TP in only 2/6 conversions. Net ~+$12.7k TP-side vs ~−$7.2k trailing-side, ambiguous and trade-dominated. Four hypotheses ranked; no discriminating test run beyond the base measurement. Refines (does not resolve) the CLAUDE.md "Trailing stop is structurally crowded out" enhancement — proposed cross-reference surfaced to the operator. Counterfactual TP levels verified correct-cycle by hand for GLW/ASTS/QQQ/SNOW.
