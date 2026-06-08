# Stop-out bleed — strategy-decided stops underperform SPY by ~7%

## Status

**observed** (2026-05-19) · **hypothesized** (2026-05-19) · untested

---

## Observation

Stop-out exits (`exit_reason='stop'`) carry materially negative excess return vs SPY across both available views of `trade_log`:

| View | n | Avg excess vs SPY |
|---|---|---|
| **Raw** (no dedup, no active-universe filter) | 526 | **−7.088%** |
| **Deduped** (latest WF run per symbol, active universe only) | 20 | **−7.451%** |

Both views show the same sign and the same approximate magnitude. The pattern survives the dedup, which means it is **not** a sampling artifact of stacked WF retrains and is **not** explained by stale rotated-out symbols. The −7% bleed is structural to how the system places and fills stops on losing trades.

The full per-exit-reason breakdown that exposes the structural nature (deduped view, 2026-05-19):

| exit_reason | n | avg_excess | cum_excess |
|---|---|---|---|
| stop | 20 | **−7.451%** | −149.02% |
| tp | 9 | +19.768% | +177.91% |
| signal_flip | 11 | +6.105% | +67.16% |
| trailing | 9 | +3.183% | +28.65% |
| fold_end | 56 | (excluded — backtest artifact per CLAUDE.md) | — |

Three of four strategy-decided exit reasons (tp, signal_flip, trailing) are positive excess. Stop is the only bucket with the wrong sign — and in the raw view it is also the second-largest bucket (only fold_end is larger, and fold_end is excluded).

SQL that surfaced this:

```sql
SELECT exit_reason,
       COUNT(*)                                AS n,
       AVG(pnl_pct - benchmark_return_pct)*100 AS avg_excess_pct
FROM trade_log
WHERE benchmark_return_pct IS NOT NULL
GROUP BY exit_reason
ORDER BY n DESC;
```

---

## Sample / dataset

- **Source**: `trade_log` on `db/trading.db` as of 2026-05-19, with `benchmark_return_pct` populated by `scripts/backfill_benchmark_returns.py`.
- **NULL benchmark filter**: zero rows excluded — SPY bar coverage (2024-04-17 → 2026-05-19) fully contains the trade_log range.
- **fold_end exclusion**: applied to headline framing. See the "Fold-end closures are backtest artifacts" architectural-decision note in CLAUDE.md.
- **Two views**:
    - *Deduped* matches the default Page 10 view (`dedup_to_latest_run=True`, `active_universe_only=True`).
    - *Raw* matches the multi-run history (`dedup_to_latest_run=False`, `active_universe_only=False`).
- **Baseline pin**: the deduped numbers are also frozen as `tests/test_trade_log.py::test_benchmark_aggregates_deduped_baseline_2026_05_19` so future drift produces visible test failures.

---

## Hypotheses (ranked)

**H1 — ATR-based stops are too tight in low-VIX regimes; stops fire on noise.**

The system places stops at `entry − atr_stop_multiplier × ATR` (default 2.0×) regardless of VIX context. In a low-VIX regime, ATR is depressed → the stop sits closer to entry in absolute price terms → normal intraday noise can reach the stop level even when the underlying thesis is intact. If H1 holds, the stop bucket's bleed should be disproportionately concentrated in low-VIX-at-entry trades.

**H2 — Long-only gate plus a rising market: entries happen on weakness, SPY rebounds, the stop fires before the long recovers.**

With `allow_short_selling=False` (default), the strategy only acts on the BUY side. In a rising market, the most BUY-eligible setups are technical pullbacks (oversold RSI, MACD inflecting up, etc.) — but SPY itself is rallying through those pullbacks. If the long's recovery takes longer than SPY's continuation, the stop fires while SPY has run further; the bleed is driven not by the long losing in absolute terms but by SPY making more in the same window. This is consistent with the documented overbought-tape diagnosis under CLAUDE.md *"Pervasive SELL bias across universe"*.

**H3 — LSTM picks dip-buys that are actually downtrends, not pullbacks.**

LSTM target is `sign(5-bar forward return)`. After a sharp decline the 60-bar input window may contain a mid-decline bounce that the model treats as the start of a recovery; if the actual structure is a continuing downtrend, the long is stopped out at the trend resumption. This is the WFC-archetype failure in `docs/case_studies/losers_2026-05.md` §1 ("bought a falling knife"). The losers case study identifies this as one of four entry archetypes; if it is also the dominant *stop bucket* driver across the wider dataset, H3 is supported.

---

## Discriminating tests

**For H1** — bucket stop trades by VIX-at-entry. If the bottom VIX quartile shows materially worse avg excess than the top VIX quartile, H1 is supported.

```sql
WITH vix_at_entry AS (
  SELECT t.id, t.pnl_pct, t.benchmark_return_pct,
         (SELECT v.close FROM ohlcv_bars v
          WHERE v.symbol='^VIX' AND v.interval='1d' AND v.timestamp <= t.entry_ts
          ORDER BY v.timestamp DESC LIMIT 1) AS vix_close
  FROM trade_log t
  WHERE t.exit_reason='stop' AND t.benchmark_return_pct IS NOT NULL
)
SELECT NTILE(4) OVER (ORDER BY vix_close)        AS vix_quartile,
       COUNT(*)                                  AS n,
       AVG(vix_close)                            AS avg_vix,
       AVG(pnl_pct - benchmark_return_pct)*100   AS avg_excess_pct
FROM vix_at_entry
GROUP BY vix_quartile
ORDER BY vix_quartile;
```

**For H2** — split stop trades by SPY direction over the holding period (already available — `benchmark_return_pct`). If trades where SPY was positive over the holding window dominate the bleed, H2 is supported.

```sql
SELECT CASE WHEN benchmark_return_pct > 0 THEN 'spy_up' ELSE 'spy_flat_or_down' END AS spy_bucket,
       COUNT(*)                                AS n,
       AVG(pnl_pct - benchmark_return_pct)*100 AS avg_excess_pct,
       AVG(pnl_pct)*100                        AS avg_trade_pct,
       AVG(benchmark_return_pct)*100           AS avg_spy_pct
FROM trade_log
WHERE exit_reason='stop' AND benchmark_return_pct IS NOT NULL
GROUP BY spy_bucket;
```

**For H3** — join `signal_log` on (symbol, bar_timestamp) closest to entry_ts, bucket by LSTM score quintile, cross-tab with entry-day RSI from `indicator_snapshots` and MACD sign. If high-LSTM-score-at-entry stops dominate the bleed AND the same trades also show entry-day high RSI or negative MACD, H3 is supported. This is also the empirical link to the losers case study's "LSTM recency saturation" cross-case insight (§4 of `losers_2026-05.md`).

```sql
-- Sketch — needs the (symbol, bar_timestamp ≈ entry_ts) proximity join.
-- See models/walk_forward.py for the bar_timestamp lookup convention.
-- Buckets to surface: LSTM quintile, RSI band (≤30 / 30-70 / ≥70), MACD sign.
```

---

## What we are NOT doing yet, and why

- **Not changing `atr_stop_multiplier`**, **not adding a regime-aware stop**, **not adding a saturation gate to entries**. The pattern is real but the *cause* is not yet diagnosed; any of the three hypotheses could be the driver, and the fix differs by hypothesis (H1 → adaptive stops by VIX, H2 → benchmark-aware stop placement or position-relative-to-SPY hold rule, H3 → entry-side LSTM-saturation or MACD-disagreement gate). Acting before the discriminator runs would be parameter tuning without a cost function.
- **Not opening a CLAUDE.md *Enhancements* entry yet.** Enhancements are for directions worth pursuing once evidence accumulates; the discriminating tests above produce that evidence and either escalate the finding into a specific Enhancement entry or eliminate one hypothesis at a time. Opening an Enhancement entry today would presuppose a direction the data hasn't supported.
- **Phase B live data is not yet available.** Simulated WF stops may differ from realized broker fills in slippage, gap-through behavior, and stop-modification timing. Diagnosing a structural pattern using *only* simulator output risks fixing the simulator rather than the strategy. Phase B realised-fill data lets the H1/H2/H3 tests run against actual broker outcomes.

---

## Trigger to revisit

Revisit when **any** of the following fires:

- Phase B (live IBKR fill reconciliation) reaches **≥50 `source='live'` closed trades**. At that point the H1/H2/H3 tests can be re-run against realized fills and the simulator-vs-live divergence can also be quantified.
- The next weekly retrain produces a materially different per-exit-reason picture — specifically, if the stop bucket's avg excess shifts by more than ±2 percentage points in either direction. That would suggest the pattern is less stable than it appears today and warrants triage before further investigation.
- Win-rate-vs-benchmark (current 51.0% in the deduped view per the 2026-05-19 baseline pin) drops below 51% on **two consecutive weekly retrains**. Would suggest the stop bleed is actively eroding the strategy's break-even and the trigger to act has fired even without full Phase B data.

---

## Status log

**2026-05-19** — Observed. Phase 3 of the benchmark-relative tracking work (Page 10) surfaced the per-exit-reason excess breakdown. Both the deduped (n=20) and raw (n=526) views showed the stop bucket at ~−7% avg excess with consistent magnitude across views, making this a structural pattern rather than a small-sample artifact. Three hypotheses ranked above; no discriminating tests run yet. The simultaneous TP-concentration observation (`tp_concentration.md`, opened the same day) is noted as a possibly-related but distinct phenomenon — the TP bucket carries strong positive excess while the stop bucket carries strong negative excess, which is itself consistent with H2 (an asymmetric long-only-in-rising-market regime affecting both ends of the bracket). Whether the two findings share a root cause is one of the open questions and may be revisited as a third finding if discriminating tests confirm the linkage.

**2026-05-27** — First live stop-out reconstructed via `reqExecutions`. SCHW entry 2026-04-23 @ $91.12 × 438 sh; held 24 trading days in a $89-94 chop range; bracket STP triggered 2026-05-27 09:46:55 ET at $85.91 after SCHW gapped through stop $85.93 in the first 17 min of RTH. Confirmed off-DB via direct IBKR `reqExecutions` query during `/daily-run-review` (exec_id `0000dc8f.6a845297.01.01`); not yet in `trade_log` because Phase B reconciliation hasn't shipped. Realised P&L: −$2,282 / **−5.7%**. SPY 4/23 → 5/27 was roughly flat, so realised excess ≈ −5.7% — sits inside the −7% headline mean. H2-relevant: held through a flat-to-slightly-up SPY window while SCHW chopped sideways then gave back ~6% in a single gap event. **n=1, does not move the headline numbers** (the 2026-05-19 baseline pins remain valid), but it is the first datapoint toward the "Phase B simulator-vs-live divergence" question in *Trigger to revisit* — when Phase B reconciliation lands and ≥50 live rows accumulate, this trade is the seed of that comparison. Cross-referenced from `docs/case_studies/losers_2026-05.md` (SCHW footnote) and `docs/reviews/followups.md` (2026-05-27 entry on the 3rd between-run invisible exit).

**2026-06-08** — **First aggregate read on realised (`source='live'`) stops: n=10, mean excess −8.33%, median −8.94%, 10/10 negative.** With Phase B reconciliation + the Flex backfills (incl. VRT/GE recovered this session), `trade_log` now holds 10 live stop-outs. The live cohort confirms the finding on *realised* fills — and bleeds ~1 pp **worse** than the 2026-05-19 *simulated* estimate (deduped −7.45% / raw −7.09%):

| id | symbol | entry → exit | pnl_pct | SPY over hold | **excess** |
|----|--------|--------------|--------:|--------------:|-----------:|
| 2000 | ABBV | 04-22 → 04-29 | −6.77% | +0.20% | −6.97% |
| 2002 | SLV* | 04-29 → 04-30 | −2.58% | −0.19% | −2.39% |
| 2005 | WFC | 04-24 → 05-08 | −5.01% | +3.72% | −8.73% |
| 2009 | TMUS | 04-29 → 05-15 | −5.16% | +4.05% | −9.21% |
| 2013 | TEL | 05-07 → 05-18 | −6.69% | +0.57% | −7.26% |
| 2015 | UAL | 05-08 → 05-19 | −9.56% | −0.41% | −9.15% |
| 2003 | SCHW | 04-23 → 05-27 | −5.68% | +5.64% | −11.32% |
| 2021 | AZN | 05-07 → 06-03 | −4.72% | +2.95% | −7.67% |
| 2023 | SLV | 05-18 → 06-05 | −8.10% | +1.38% | −9.48% |
| 2186 | VRT | 05-22 → 06-05 | −10.72% | +0.43% | −11.15% |

`*` id=2002 is the orphaned-short SLV episode (labeled `exit_reason='stop'`, the smallest bleed); the other 9 are long stops. SCHW's excess is now precisely **−11.32%** (SPY ran +5.64% during the 24-day hold) — note the 2026-05-27 entry above cited −5.7%, which was the *raw* P&L, not the excess; the excess is more than double once SPY's run is netted.

**Decomposition (the headline bleed splits into two distinct drivers):** mean realised loss ≈ **−6.5%** (the longs genuinely fell to their stops) plus mean SPY drift ≈ **+1.8%** over the holds (SPY rose while the long bled). So the −8.3% excess is *mostly* H1/H3 (real declines triggering the stop) with H2 (SPY outrunning a stopped long) contributing ~1.8 pp on top — **not** a pure H2 effect. UAL is the clean counter-example to a pure-H2 reading: SPY was *down* −0.41% over its hold yet excess was still −9.15%, because UAL itself dropped −9.56%. The H2 amplifier is real but secondary; the dominant term is that these were genuine losers, consistent with H1 (too-tight ATR stops firing on real moves) and H3 (dip-buys that were downtrends).

**Does not yet satisfy the *Trigger to revisit*** (which wants ≥50 live *closed* trades, not 10 live *stops*) — but it converts the SCHW n=1 seed into a 10-point realised cohort and answers the simulator-vs-live divergence direction: **live stops bleed slightly worse than simulated**, so the simulator is, if anything, *under*-stating the problem. Re-run the H1/H2/H3 discriminating SQL against the live subset once the cohort reaches ~30. (Surfaced during the 2026-06-08 VRT case-study triage — VRT did not warrant a standalone case study but is a clean member of this distribution; see the daily-review thread.)
