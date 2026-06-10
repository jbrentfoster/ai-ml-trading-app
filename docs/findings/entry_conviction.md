# Entry conviction — do low-margin BUYs ("squeakers") underperform high-conviction BUYs?

## Status

**hypothesized** (2026-06-10) · distributional observation pending (sample too thin to confirm) · untested

---

## Origin

Raised by the operator on 2026-06-10 after reviewing Trade Forensics for the 2026-06-09 stop-outs: "most of these barely squeaked over for a buy." The standing instinct across the project (see CLAUDE.md *"Pervasive SELL bias"* 2026-05-13 note) is **not** to tune the system toward *more* BUY signals; this finding asks the opposite question — should the BUY gate be made *harder*, dropping the lowest-conviction entries?

The operator pre-empted his own counterpoint: several big winners also barely cleared the gate. That counterpoint does **not** refute the hypothesis. The existence of squeaker-winners is only relevant via the *conditional expectancy* of the low-margin band. The decision rule is:

> raise the bar **iff** `E[excess | low margin] < E[excess | high margin]` by enough to justify forgoing the squeaker-winners that the higher bar would also cut.

"Do any squeakers win?" is the wrong question. "What is the mean (and dispersion of) excess return in the bottom margin band, net of the winners it contains?" is the right one.

---

## Observation (preliminary — n=22, NOT yet distributional)

Entry ensemble score for every `source='live'` BUY trade that could be matched to a passed-gate BUY row in `signal_log` at/before `entry_ts` (22 of 34 live BUYs matched; the other 12 are Flex-backfilled / pre-logging rows with no `signal_log` anchor — a data-availability caveat, see *Sample*). Sorted by entry ensemble score:

| ens@entry | symbol | exit_reason | pnl% | excess% |
|----------:|--------|-------------|-----:|--------:|
| 0.526 | TEL  | stop        | −6.69 | −7.26 |
| 0.535 | AZN  | stop        | −4.72 | −7.67 |
| 0.555 | AON  | trailing    | +0.79 | −0.02 |
| 0.557 | MMM  | signal_flip | +1.97 | +3.49 |
| 0.564 | VRT  | stop        | −10.72 | −11.15 |
| 0.590 | SLV  | stop        | −8.10 | −9.48 |
| 0.601 | CL   | manual_close| +2.96 | +2.56 |
| 0.607 | AXTI | stop        | −22.21 | −21.45 |
| 0.609 | GE   | signal_flip | +15.06 | +13.75 |
| 0.609 | AAOI | signal_flip | +7.47 | +5.85 |
| 0.613 | QQQ  | trailing    | +1.67 | +1.24 |
| 0.617 | SNDK | trailing    | +25.04 | +23.66 |
| 0.621 | ON   | stop        | −11.05 | −10.98 |
| 0.625 | GLW  | trailing    | +5.98 | +7.26 |
| 0.641 | GLD  | signal_flip | +0.92 | −0.34 |
| 0.641 | SPY  | tp          | +2.39 | +0.35 |
| 0.656 | SYY  | signal_flip | +1.54 | +0.10 |
| 0.664 | NXPI | stop        | −7.81 | −5.53 |
| 0.667 | MRVL | tp          | +24.77 | +24.67 |
| 0.668 | LITE | signal_flip | +2.62 | +0.46 |
| 0.677 | GEV  | stop        | −9.43 | −9.22 |
| 0.677 | UAL  | stop        | −9.56 | −9.15 |

Rough tercile split on mean excess:

| ens@entry band | n | mean excess% |
|---|---|---:|
| 0.526–0.601 (bottom) | 7 | **−4.22** |
| 0.607–0.641 (middle) | 8 | **+2.37** |
| 0.641–0.677 (top)    | 7 | **+0.24** |

**What this preliminary cut says — and what it does NOT:**

- The lowest-conviction band leans *negative* (−4.22% mean excess) while the middle/top are flat-to-positive — **weakly consistent** with the operator's instinct.
- But the relationship is **non-monotonic and noisy**: the top band is dragged down by GEV/UAL/NXPI stops (high ensemble score, still lost), and the middle band is rescued by SNDK/GE winners. Raw ensemble score does **not** cleanly separate winners from losers at n=22.
- The single worst trade (AXTI, −21.45% excess) sits in the *middle* band, not the bottom.

The takeaway is therefore **not** "squeakers lose" — it is "raw ensemble margin is too blunt an axis to settle this, and the sample is far too small." See Hypotheses for the sharper axes.

SQL that surfaced the table:

```sql
SELECT t.symbol, t.entry_ts, t.exit_reason, t.pnl_pct, t.benchmark_return_pct,
  (SELECT s.ensemble_score FROM signal_log s
     WHERE s.symbol=t.symbol AND s.signal='BUY' AND s.passed_gate=1
       AND s.bar_timestamp <= t.entry_ts
     ORDER BY s.bar_timestamp DESC LIMIT 1) AS entry_ens
FROM trade_log t
WHERE t.source='live' AND t.signal='BUY'
ORDER BY entry_ens;
```

---

## Sample / dataset

- **Source**: `trade_log` `source='live'` on `db/trading.db` as of 2026-06-10. 34 live closed BUY trades total; 22 matched an entry `signal_log` row.
- **Match gap (12/34 unmatched)**: Flex-backfilled live rows (`scripts/backfill_flex_trades.py`) and any pre-`signal_log`-logging rows have no passed-gate BUY anchor. As Phase B accumulates organically-reconciled trades (which *do* have a same-run `signal_log` row), the match rate should climb. Until then the 22-row view is biased toward symbols the daily runner entered itself.
- **"Margin" proxy used here is RAW ensemble score, not margin over the effective gate threshold.** This is the crude first cut. The effective threshold is regime-adjusted (`signal_gate.py`: HIGH_VOLATILITY ×1.5, TRENDING ×`trending_threshold_multiplier`=1.2 against base `signal_threshold`=0.35) — so a 0.526 entry in a HIGH_VOLATILITY regime (effective bar ≈0.525) is a true squeaker, while a 0.526 entry in a calm regime clears 0.35 comfortably. The raw score conflates these. Computing the real per-trade margin requires reconstructing the regime at entry — that is part of the deferred analysis, not done here.
- **Not pinned by a baseline test** — the sample is too small and too unstable to freeze. Pin only once it crosses the *Trigger to revisit* threshold.

---

## Hypotheses (ranked)

**H1 — Margin over the *effective (regime-adjusted)* threshold discriminates outcome better than raw ensemble score.**

The preliminary table shows raw ensemble score barely sorts winners from losers. But the gate's *effective* bar moves with regime. A trade that cleared 0.35 by a hair in a HIGH_VOLATILITY regime (effective ≈0.525) is the real "squeaker"; raw score hides that. If margin-over-effective-threshold separates the buckets where raw score did not, H1 holds and the lever is a higher base `signal_threshold` (or a regime-floor on margin).

**H2 — It is not the *size* of the margin but *which model* supplies it.**

A BUY where all three models weakly agree is a different setup from one where LSTM is strong but XGBoost drags the ensemble down to barely-over (the exact mechanism behind the 2026-05-11 SELL-bias diagnosis), or where FinBERT props a price-bearish ensemble over the line (the AZN failure archetype — CLAUDE.md *"LSTM-saturated-bearish held longs"* note; note AZN appears here at ens 0.535, a bottom-band stop). If outcome correlates with *component agreement / which model is pivotal* rather than aggregate margin, the lever is a feature-disagreement gate (already parked: CLAUDE.md *"LSTM ↔ MACD direction-disagreement gate"* enhancement), not a higher scalar threshold.

**H2-FinBERT — FinBERT scores on mis-attributed headlines are noise and may tip borderline gate decisions.** (operator hypothesis, 2026-06-10)

The Page 11 LLM-analyst work proved IBKR symbol-tagged news is frequently about a *different* company (7 of 8 NVDA/AAPL-tagged test articles were about Marvell/Broadcom/Dell/HPE — see `news_attribution_misallocation.md`). FinBERT scores headlines under the *feed* symbol with no attribution check, so a mis-attributed headline injects another company's sentiment into this symbol's ensemble. If that noise flips HOLD→BUY (threshold or confirmation), it manufactures low-quality entries.

**Status on the BUY-entry sample (n=22, tested 2026-06-10): NOT supported — but under-sampled where it would bite.** Component scores at the 22 matched live BUY entries show: (a) **LSTM dominant** — 18/22 have LSTM ≥ 0.87, so BUYs are essentially LSTM-conviction calls; (b) **FinBERT small** — magnitude mostly 0.02–0.38 (only CL 0.77, SYY 0.93 large; both non-losers), and with the smallest ensemble weight (~0.25 × coverage) its score contribution is ~0.02–0.08; (c) **FinBERT pivotal for the 2-of-3 confirmation filter in only 1/22** (QQQ, XGBoost −0.08 → LSTM+FinBERT carried it — and QQQ *won*, +1.24% excess); (d) the borderline losers (TEL, AZN, VRT) clear their regime-adjusted bar by margins (≈0.11 for TEL over its 0.42 TRENDING threshold) that survive stripping FinBERT and redistributing its weight. So on the BUYs we can observe, FinBERT is *not* the thing tipping entries over — LSTM is.

Three reasons the sample can't yet refute the operator's broader point, each a place FinBERT noise *would* be decisive:
- **No HIGH_VOLATILITY entries in the sample** (all 22 are MEAN_REVERTING / TRENDING). High-vol raises the effective bar to 0.525, where a small FinBERT contribution is far more likely to be the deciding term.
- **The SELL / hold-suppression side is invisible to a BUY-only cut.** The documented AZN archetype is FinBERT *propping* a price-bearish ensemble above the SELL gate and suppressing an exit — plausibly where mis-attribution does the most damage (riding a loser down), and not testable here.
- **Conviction-magnitude pollution without a gate flip.** Where FinBERT is large (CL 0.77, SYY 0.93), a mis-attributed headline inflates the *ensemble score* even though the gate decision wouldn't change — corrupting the H1 margin axis and any future realised-Kelly sizing that reads ensemble score.

Discriminating test specific to H2-FinBERT below.

**H3 — There is no usable conviction signal in the score at all; outcome is dominated by post-entry regime / stop mechanics.**

The stop-bleed finding (`stop_bleed.md`, 2026-06-08: n=10 live stops, all negative, mean −8.3% excess) shows stops bleed regardless of entry. If the bottom-band negativity here is just "stops fire on everything and the bottom band happened to contain more stops," then entry conviction is a red herring and the real lever is on the *exit* side (stop placement), not the entry gate. This is the null hypothesis and must be ruled out before raising the BUY bar — otherwise we would tighten entries and the surviving high-conviction trades would *still* bleed at the stop.

---

## Discriminating tests

**For H1** — recompute the margin as `ensemble_score − effective_threshold_at_entry`, where the effective threshold reconstructs the regime multiplier (read `signal_log.regime` at the entry bar; apply `trending_threshold_multiplier` / `high_vol_threshold_multiplier`). Bucket by NTILE(4) on that margin; compare mean excess and win-rate-vs-benchmark across quartiles. H1 supported if the bottom margin quartile shows materially worse mean excess than the top **and** the separation is cleaner than the raw-score tercile split above.

**For H2** — for each live BUY, pull `lstm_score`, `xgb_score`, `finbert_score` from the entry `signal_log` row. Define "pivotal model" = the one whose removal would drop the ensemble below the effective threshold. Cross-tab outcome (win / excess) by pivotal model and by a 3-model-agreement flag (all same sign vs mixed). H2 supported if FinBERT-pivotal or XGBoost-dragged entries underperform unanimous entries at similar aggregate margin.

**For H2-FinBERT** — two cuts. (1) *Gate-flip count*: across all gate decisions (not just fills), count how often FinBERT is the pivotal term — i.e. removing it (score→0, weight redistributed) would drop the ensemble below the effective threshold **or** break the 2-of-3 confirmation. On the n=22 live-BUY sample this was 1/22; re-run as the sample grows and split by regime (expect it to rise in HIGH_VOLATILITY). (2) *Attribution overlay*: once `llm_news_analysis` has coverage, join each entry's FinBERT-scored articles to the LLM attribution status (`matched` / `reattributed` / `untracked` / `digest`). H2-FinBERT supported if FinBERT-pivotal entries are disproportionately backed by `reattributed`/`untracked` headlines (i.e. the score that tipped the gate was computed on a different company's news). This is the direct link to `news_attribution_misallocation.md` — and the cleanest fix it would imply is attribution-gating FinBERT (drop or down-weight articles whose LLM `attributed_symbol` ≠ the feed symbol) rather than touching the scalar threshold.

**For H3** — restrict to live BUYs and regress (or just bucket) excess on entry margin **while holding exit_reason fixed** (i.e. within the `stop` subset only, does higher entry conviction reduce the bleed?). If entry margin has no effect *within* the stop bucket, H3 holds and the entry gate is not the lever — `stop_bleed.md` owns the problem. Cross-reference: this is the explicit linkage test between the two findings.

---

## What we are NOT doing yet, and why

- **Not raising `signal_threshold`, not adding a margin floor, not adding a feature-disagreement gate.** The n=22 preliminary cut does not even establish the distributional pattern, let alone its cause. The three hypotheses imply three *different* levers (H1 → scalar threshold, H2 → disagreement gate, H3 → don't touch entries at all, fix stops). Acting now is parameter-tuning without a cost function — the exact mistake the SELL-bias note warns against in the opposite direction.
- **Not treating raw ensemble score as "conviction."** The effective-threshold margin (H1) is the honest axis; raw score conflates regimes. Any analysis that skips the regime reconstruction will produce a misleading answer.
- **Not opening a CLAUDE.md Enhancement entry.** The discriminating tests either escalate this into a specific Enhancement (e.g. "raise base threshold," or feed H2 into the existing LSTM↔MACD gate enhancement) or eliminate a hypothesis. Pre-committing a direction presupposes an answer the data hasn't given.
- **Mindful of the standing prior**: the project has repeatedly chosen *not* to manufacture activity. Making BUYs harder is the conservative direction (less activity, less risk) and is consistent with that prior — but "conservative" is not "free." The forgone squeaker-winners (MRVL was a +24.67% excess winner at ens 0.667 — *not* a squeaker by raw score, but the point stands for whatever the true squeaker band turns out to be) are a real cost that the expectancy comparison must price in.

---

## Trigger to revisit / verification gate

Revisit when **any** fires:

- **`source='live'` BUY trades with a matched `signal_log` entry row reach ≥50** (currently 22). At that point the H1/H2/H3 tests have enough power to run, and the match rate should be higher (organic Phase B trades carry their own `signal_log` row, unlike the Flex backfills). This is the primary gate.
- A weekly/daily review notes that the **bottom margin band's mean excess stays negative across two successive accumulation ticks** while the top band stays positive — i.e. the preliminary signal here strengthens rather than washing out. That would justify pulling the H1 test forward even before n=50.
- The **stop-bleed finding resolves** (`stop_bleed.md`). If H3-for-stops is settled there first, this finding's H3 collapses into it and the entry-conviction question simplifies to H1 vs H2 on the non-stop exits.

---

## Status log

**2026-06-10** — Opened. Triggered by the operator's read of the 2026-06-09 stop-outs ("most barely squeaked over"). Pulled the n=22 matched-live-BUY entry-ensemble-vs-excess table above. Preliminary tercile split shows the lowest-conviction band at −4.22% mean excess vs +2.37% / +0.24% for middle/top — weakly consistent with the hypothesis but **non-monotonic and far too small to act on** (worst single trade AXTI is mid-band; top band dragged by GEV/UAL/NXPI stops). Key reframing recorded: raw ensemble score is the wrong axis (conflates regimes); the honest axis is margin over the *effective regime-adjusted* threshold (H1), and the deeper question is whether *which model is pivotal* matters more than scalar margin (H2). Explicit linkage to `stop_bleed.md` via H3 — if entry conviction has no effect *within* the stop bucket, the lever is on exits, not entries. No code change; no threshold touched. Primary trigger: ≥50 matched live BUYs (from 22). Cross-references: CLAUDE.md *"Pervasive SELL bias"* (the don't-manufacture-activity prior), *"LSTM ↔ MACD direction-disagreement gate"* enhancement (H2 lever), *"LSTM-saturated-bearish held longs"* note (AZN archetype), and `stop_bleed.md` (H3).

**2026-06-10** (same session) — Operator hypothesis: FinBERT on mis-attributed headlines is garbage and may be tipping borderline signals over the gate. Tested directly against the 22-entry component scores (added as **H2-FinBERT** above). **Not supported on the BUY-entry sample**: LSTM dominates (18/22 ≥ 0.87), FinBERT magnitude is small (mostly 0.02–0.38) and its weighted contribution (~0.02–0.08) doesn't move the observed margins; FinBERT was the pivotal confirmation vote in only 1/22 (QQQ, a *winner*). The borderline losers (TEL/AZN/VRT) clear their regime bar by margins that survive stripping FinBERT. **But the BUY-only sample is structurally blind to the three places FinBERT noise would bite**: HIGH_VOLATILITY regimes (none in sample; effective bar 0.525 makes small terms decisive), the SELL/hold-suppression side (AZN archetype — riding a loser down because FinBERT props the ensemble above the SELL gate), and conviction-magnitude pollution where FinBERT is large (CL 0.77 / SYY 0.93 — inflates ensemble score even without flipping the gate, corrupting the H1 margin axis and future Kelly inputs). Discriminating test specified (gate-flip count by regime + an `llm_news_analysis` attribution overlay); the fix it would imply is **attribution-gating FinBERT** (down-weight/drop articles whose LLM `attributed_symbol` ≠ feed symbol), not a scalar threshold change. Direct cross-link established to `news_attribution_misallocation.md`. The attribution overlay is gated on `llm_news_analysis` coverage, which is itself gated on scheduling `run_llm_news.bat` (CLAUDE.md Enhancement, not yet wired into Task Scheduler). *[Superseded same day — see 2026-06-10 (coverage) log entry below.]*

**2026-06-10** (coverage update) — `run_llm_news.bat` is in fact scheduled and has been running daily since 2026-06-02 (CLAUDE.md corrected). `llm_news_analysis` now holds **1,538 rows** spanning 2026-06-02 → 2026-06-09 across 70 feed symbols, all `parse_ok=1`. Attribution split: **626 `matched` / 397 `reattributed` / 515 `untracked`-or-`digest`** — only **~41%** of feed-tagged scored articles are actually about the feed symbol. That directly substantiates the *premise* of H2-FinBERT (FinBERT scores all 100% of feed-tagged articles with no attribution check, so ~59% of what it scores is another company's or a multi-company digest's sentiment). **The H2-FinBERT attribution-overlay discriminating test is no longer blocked** — the data exists. It is still not *run* (the full join from a signal_log entry's FinBERT score back to its constituent articles' attribution status is part of the deferred ≥50-live-BUY analysis, not done here), but the gate is data-availability and that gate is now open. Note the ~59% non-match rate is a data-quality finding in its own right regardless of whether it tips gate decisions — it pollutes the FinBERT component everywhere it is non-zero, which is the conviction-magnitude / hold-suppression concern, not just the BUY-gate-flip concern.
