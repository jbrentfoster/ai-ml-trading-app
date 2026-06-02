# TP concentration — 18% of trades carry the entire alpha

## Status

**observed** (2026-05-19) · **hypothesized** (2026-05-19) · untested

---

## Observation

In the deduped strategy-decided view (Page 10 default), **9 of 49 trades carry the entire +124.69% cumulative excess return**. Those 9 are all `exit_reason='tp'` exits with an average excess of **+19.768%** each. The remaining 40 trades, taken together, net negative.

Decomposition of the deduped strategy-decided +124.69% cumulative excess (2026-05-19):

| Bucket | n | Avg excess | Contribution |
|---|---|---|---|
| **tp** | **9** | **+19.768%** | **+177.91%** |
| signal_flip | 11 | +6.105% | +67.16% |
| trailing | 9 | +3.183% | +28.65% |
| stop | 20 | −7.451% | −149.02% |
| **Total** | **49** | +2.545% avg | **+124.70%** |

If the 9 TP exits are removed from the population, the remaining 40 trades sum to **−53.21% cumulative excess** — a net-negative strategy. The entire alpha narrative rests on the right tail.

SQL that surfaced this (same shape as the `stop_bleed.md` query):

```sql
SELECT exit_reason,
       COUNT(*)                                AS n,
       AVG(pnl_pct - benchmark_return_pct)*100 AS avg_excess_pct,
       SUM(pnl_pct - benchmark_return_pct)*100 AS cum_excess_pct
FROM trade_log
WHERE benchmark_return_pct IS NOT NULL
  AND exit_reason != 'fold_end'
-- plus dedup filter applied in Python via _keep_latest_run_per_symbol
GROUP BY exit_reason;
```

---

## Sample / dataset

- **Source**: `trade_log` on `db/trading.db` as of 2026-05-19, with `benchmark_return_pct` populated by `scripts/backfill_benchmark_returns.py`.
- **View**: deduped + active universe (Page 10 default). n=49 strategy-decided trades after fold_end exclusion.
- **Raw view sanity check**: 121 TP trades at +9.033% avg excess, +1092.99% cumulative. The right-tail concentration holds in the raw view too — the TP bucket carries the alpha there as well, just at lower per-trade magnitude because of cross-retrain dedup overlap. The deduped view's higher +19.768% avg is consistent with "latest-run-per-symbol" picking the most-recent retrain's best-case exits.
- **Baseline pin**: the deduped and raw aggregates are frozen as `tests/test_trade_log.py::test_benchmark_aggregates_*_baseline_2026_05_19` so future drift produces visible test failures. The TP-bucket numbers are not separately pinned today but can be added if drift surfaces.

---

## Hypotheses (ranked)

**H1 — A small subset of high-quality setups is the entire alpha; the rest is noise around zero or losing.**

The strategy's edge — if it has one — lives in a narrow distribution of setups that genuinely beat SPY by a wide margin, with the rest of the trade book being approximately break-even-minus-costs noise. This is the standard "trading is fat-tailed" pattern; almost all profitable systematic strategies have it. Under H1 the 9 winners are real but cannot be identified ex ante from the features currently available to the gate.

**H2 — The 9 winners share a common context (regime / sector / entry signature) that could be filtered for.**

A specific sub-hypothesis of H1: not just that there are good setups and bad setups, but that the good setups are *identifiable in advance* by features the gate doesn't currently use. Candidates: TRENDING regime + RSI in a specific band, sector concentration in growth/tech, entry-day ensemble score above a higher threshold than the current 0.35, news cluster intensity (per `axti_2026-04.md` §5b), Stage 3 score above some higher floor. If H2 holds, the 9 TP-winner entries cluster tightly on at least one of these dimensions while the 40 others don't.

**Candidate discriminating features for H2 (qualitative priors from the winner case studies, 2026-06-02).** A cross-read of the five realized winners (`axti_2026-04.md` +38.7%, `mrvl_2026-05.md` +24.8%, `asts_2026-04.md` +23.4%, `snow_2026-04.md` +17.5%, AON +1.2% [thin]) against the `losers_2026-05.md` cohort (WFC / TMUS / UAL) surfaces a candidate feature shortlist to check *first* when the H2 cluster query below is run. These are priors from named trades (case-study unit, n≈5), **not** a distributional result — they exist to focus the discriminator, not to pre-judge it:

- **Entry RSI not saturated / not at a local top.** The cleanest winner-vs-loser split in the case set. Every loser cleared the gate with **LSTM ≥0.90 on a stock that had just run up sharply** (recency saturation — see `losers_2026-05.md` §4 "LSTM recency saturation on momentum entries"). The magnitude winners entered with room: ASTS at RSI 38.5 (below EMA-50, near lower Bollinger), MRVL after a pullback ($217→$198→$205), AXTI at a moderate RSI 55.7. Test: is entry-bar RSI (and LSTM-saturation) *lower* on the TP-winners than on the non-TP population?
- **News-cluster intensity at entry** (article count × |avg sentiment| over the entry window). AXTI: ~24 articles in a 3-day "optical AI" cluster (`axti_2026-04.md` §5b). MRVL: earnings cluster 43→71 articles. Losers and the thin winner (AON) were catalyst-free entries.
- **Analyst PT at/above entry (not stale-below).** MRVL's analyst target $215 sat *above* the $205 entry — rare; usually the PT is stale-below. AXTI's PT was stale at entry but re-rated +30% mid-hold. Losers carried stale PTs. Test: PT-minus-entry as a continuous feature.
- **High *relative* ATR (ATR / price) as the magnitude driver.** AXTI ATR ≈13% of price, ASTS ≈10%, MRVL ≈6%; the thin winner AON was a low-vol compounder (≈2.5%) and converted its trail correctly yet made only +1.2%. Caveat: partly tautological ("volatile names move more") — it is only *edge* if TP-winners are over-represented among high-relative-ATR names *beyond* their base rate in the universe. The cluster query should test relative-ATR against the non-TP population, not in isolation.

**Survivorship caveat specific to these priors.** The two largest winners (AXTI, MRVL) were **overnight gaps through the TP** — magnitude is timing luck, not selection skill (both case studies say so explicitly). So the magnitude these features "predict" is partly unrepeatable; the testable claim is the weaker one — that the features predict *participation in a setup that beats SPY*, not the size of the gap. This is also why the inverse — CSCO (`csco_2026-05.md`), the system's biggest *miss* — matters: the "don't buy overbought" thread that separates winners from losers is the same XGBoost mean-reversion logic that gated out a +55.9% trend. Any H2-derived filter must be regime-aware or it amplifies the CSCO failure while fixing the loser one.

**H3 — Survivorship; the 9 winners are luck and won't reappear in Phase B live data.**

The 9 are concentrated in WF-simulator output; none are realized broker fills. The simulator places bracket TPs at `entry + 3.0 × ATR` and fills them when the daily bar high crosses the level. In live trading, bracket TPs are LMT orders subject to gap-fills, partial fills, and the kinds of microstructure friction the simulator ignores. If the realized TP exits in Phase B come in at materially lower per-trade excess than +19.768%, H3 gains weight and the WF +124% becomes a simulator artifact rather than evidence of alpha.

---

## Discriminating tests

**For H1 and H2 — cluster the 9 TP-winners.** Join the 9 TP rows to `signal_log` (ensemble/LSTM/XGB/FinBERT scores at entry, regime), `indicator_snapshots` (RSI, MACD, ATR, BB position), `universe_assets` (Stage 3 score, sector via `_SECTOR_MAP` in `risk/portfolio_guard.py`). Compare the 9 TPs against the 40 non-TPs on every dimension via two-sample t-tests or visual scatter.

If a tight cluster emerges in any single dimension or 2-D projection, H2 is supported. If the 9 are scattered across the same distribution as the 40, H1 holds but H2 is rejected — and the implication is that the right tail is real but not actionable from current features.

```sql
-- Sketch — needs signal_log + indicator_snapshots join on (symbol, bar_timestamp ≈ entry_ts).
SELECT t.symbol, t.entry_ts, t.exit_reason,
       (t.pnl_pct - t.benchmark_return_pct)*100 AS excess_pct,
       s.ensemble_score, s.lstm_score, s.xgb_score, s.finbert_score, s.regime,
       i.rsi_14, i.macd, i.atr_14, i.bb_upper, i.bb_lower
FROM trade_log t
LEFT JOIN signal_log s
       ON s.symbol = t.symbol
      AND s.bar_timestamp <= t.entry_ts
LEFT JOIN indicator_snapshots i
       ON i.symbol = t.symbol
      AND i.timestamp <= t.entry_ts
      AND i.interval = '1d'
WHERE t.benchmark_return_pct IS NOT NULL
  AND t.exit_reason != 'fold_end'
ORDER BY excess_pct DESC;
```

**For H3 — compare to realized live fills.** Once Phase B accumulates `source='live'` TP exits, compute the avg excess and compare against the WF +19.768%. If the live avg comes in below ~+10%, H3 gains substantial weight. If it lands near +19.768%, H3 is rejected and the WF simulator is calibrated for TP exits at least.

```sql
SELECT source,
       COUNT(*)                                AS n,
       AVG(pnl_pct - benchmark_return_pct)*100 AS avg_excess_pct,
       MIN(pnl_pct - benchmark_return_pct)*100 AS min_excess_pct,
       MAX(pnl_pct - benchmark_return_pct)*100 AS max_excess_pct
FROM trade_log
WHERE exit_reason = 'tp'
  AND benchmark_return_pct IS NOT NULL
GROUP BY source;
```

---

## What we are NOT doing yet, and why

- **Not adding a "high-quality-setup" filter to the entry gate.** Sample size of 9 winners is too small to support feature selection without overfitting — any cluster identified in n=9 has a high false-discovery rate. The H1/H2 discriminator above produces the cluster hypothesis; acting on it requires either more WF data (multiple retrains compounding) or Phase B live data.
- **Not tuning the bracket TP multiplier.** The current 3.0× ATR is the level at which these 9 trades fired. Raising or lowering it would change which trades hit the TP; without first understanding *why* these 9 worked, parameter tuning would be search without a cost function.
- **Not concluding "the strategy has alpha" from this snapshot.** A +124% cumulative excess concentrated in 9 of 49 trades is precisely the shape that survivorship and selection bias tend to produce in small samples. The architectural-decision note on dedup-vs-raw views (CLAUDE.md) already documents this: the same fold_end-excluded slice produces −20.4% cumulative excess in the raw multi-run view. The deduped view's positivity is a function of which model version's outputs the dedup keeps.

---

## Trigger to revisit

Revisit when **any** of the following fires:

- Phase B reaches **≥5 `source='live'` TP exits**. This is the minimum sample for the H3 simulator-vs-live comparison to be informative. The live TP avg excess vs the +19.768% WF avg is the discriminator.
- A future weekly retrain produces **≥15 TP trades in the deduped view** (current: 9). At n=15 the cluster analysis for H1/H2 becomes meaningfully tractable; at n=9 it is exploratory.
- The deduped cumulative excess **flips sign** (becomes negative) on a future retrain *without* the TP bucket changing materially. Would strengthen the H1 framing (the alpha really is the 9 winners; everything else is dragging it down further).

---

## Status log

**2026-05-19** — Observed. Surfaced during Phase 2 of the benchmark-relative tracking work — the per-exit-reason table on Page 10 made the right-tail concentration immediately visible. 9 of 49 strategy-decided trades carry the entire +124.69% cumulative excess; the other 40 net negative. Three hypotheses ranked above; no discriminating tests run yet. Cross-referenced with `stop_bleed.md`: the stop-bucket bleed and TP-bucket concentration are likely related — both are consistent with an asymmetric long-only-in-rising-market regime that the strategy's bracket placement amplifies (tight stops fire often and lose to SPY; wide TPs fire rarely but capture the right tail). Whether they share a *root* cause is itself one of the open questions and may be revisited as a third finding if discriminating tests confirm the linkage.

**2026-06-02** — Enriched H2 with a qualitative entry-feature shortlist distilled from the five realized winner case studies (AXTI / MRVL / ASTS / SNOW / AON) read against the `losers_2026-05.md` cohort. No discriminating test run — these are *priors to focus the H2 cluster query*, not results (case-study unit, n≈5; below this finding's own action bar of ≥15 deduped TP trades / ≥5 live TP exits). Candidate features: entry RSI not saturated, news-cluster intensity, analyst-PT-at/above-entry, high relative ATR — with an explicit survivorship caveat (the two largest winners were overnight gap-throughs = magnitude luck) and a regime-awareness caveat (the "don't buy overbought" winner/loser separator is the same logic that produced the CSCO miss — `csco_2026-05.md`). Prompted by a session reviewing the new capital-weighted ROI surface on Page 10; the operator asked whether the cross-case synthesis warranted a *new* findings doc — judgment was that it did not (wrong unit of analysis + duplicates H2), so it lives here instead. A standalone `big_winners.md` becomes appropriate once Phase B accumulates enough `source='live'` winners to reason distributionally rather than from named trades.
