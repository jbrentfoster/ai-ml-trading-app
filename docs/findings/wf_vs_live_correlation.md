# Walk-forward Sharpe vs. realised live outcome — weak positive, not a veto

## Status

**observed** (2026-06-01) · **hypothesized** (2026-06-01) · untested

Reproduce with: `python scripts/analyze_wf_vs_live.py`

---

## Observation

A symbol's walk-forward Sharpe has a **weak-to-moderate positive** relationship with how its live trades actually turn out — *but only when measured point-in-time*, and **not** strongly enough to use as a go/no-go threshold.

Point-in-time matching (each live trade joined to the most recent WF run for that symbol recorded **before** the trade's `entry_ts`, mean fold Sharpe):

| Measure | Value |
|---|---|
| Pearson corr (WF Sharpe vs live `pnl_pct`) | **+0.348** (t=1.70, **NOT** significant at p<0.05) |
| Spearman rank corr | **+0.393** |
| Pearson corr excluding 2 outlier winners (AXTI, ASTS) | +0.253 |

Split by sign of the pre-trade WF Sharpe:

| Bucket | n | Live win rate | Mean live return | Total P&L |
|---|---|---|---|---|
| WF Sharpe ≤ 0 | 18 | 50.0% | +0.41% | +$2,622 |
| WF Sharpe > 0 | 5 | 80.0% | +10.22% | +$20,208 |

**The methodology is load-bearing.** Matching each trade to the symbol's *latest* WF run instead (some recorded weeks after the trade — hindsight) collapses Pearson to **+0.049** (≈ noise). The entire signal lives in the point-in-time bound. Any future re-run must preserve `recorded_at <= entry_ts`.

**Two findings that matter more than the headline correlation:**

1. **Negative WF Sharpe is a poor veto.** 8 of 13 live winners had a *negative* pre-trade WF Sharpe — including ASTS (+23.9%, WF −0.69), AAOI (+7.5%, WF −0.76), SPY (+2.4%, WF −2.02), LITE (+2.6%, WF −2.39), CL (+3.0%, WF −5.9), and GLD (won twice, WF −9.3 / −0.38). A "skip anything with negative WF Sharpe" rule would have killed most of the book's gains. This is consistent with the documented conservatism of the WF cost model (post-cost WF Sharpes run negative even for names that trade fine live — see CLAUDE.md *"Consider walk-forward Sharpe in signal generation"*).

2. **Clean counterexamples exist in both directions.** UAL — the single worst live loser (−9.6%, stopped) — had a *positive* pre-trade WF Sharpe (+0.19). GLD — the *most* negative WF Sharpe (−9.3) — was a winner. The relationship is a tendency, not a rule.

This is the quantified answer to the originating question (2026-06-01 chat, re: VRT showing a strong BUY despite deeply negative WF folds): **WF Sharpe and the live signal are weakly correlated; WF Sharpe is more useful as a relative ranking than as an absolute threshold.**

---

## Sample / dataset

- **Source**: `trade_log` `source='live'` rows on `db/trading.db` as of 2026-06-01 — **n=23 closed round trips** across 22 symbols. These are the Phase B / Flex-backfill realised fills (see CLAUDE.md *"Between-run bracket/trail/TP fills are invisible…"* status and the 2026-05-31 Flex backfill).
- **WF side**: `walk_forward_results`, aggregated to one Sharpe per `(symbol, run_id)` by mean over folds, then the latest such run with `recorded_at <= entry_ts` per trade.
- **Match rate**: 23/23 — every live trade had at least one prior WF run.
- **No dedup / no benchmark filter applied** — this analysis is per-trade live outcome, not the Page 10 deduped excess view. `pnl_pct` here is net trade return, not excess-vs-SPY (deliberately — the question is "does WF predict the trade's own outcome", not "…its alpha").

**Why this is a finding, not yet actionable:** n=23 is below significance (the +0.348 Pearson has t=1.70, p≈0.10), the dollar result is dominated by two outliers (AXTI +$15.9k, ASTS +$9.5k), and exit mechanics are a heavy confound (winners cluster in `tp`/`trailing`, losers almost entirely in `stop` — see `stop_bleed.md`). The pattern is real enough to record and re-test, not strong enough to wire into the gate.

---

## Hypotheses (ranked)

**H1 — WF Sharpe carries genuine but small cross-sectional signal; it is swamped at n=23 by exit-mechanic noise.**

The rank correlation (Spearman +0.39 > Pearson +0.35) suggests the *ordering* is more reliable than the magnitudes, which is what you'd expect if WF captures a real but noisy quality axis. If H1 holds, the correlation should *persist and tighten* (not regress to 0) as n grows, and should strengthen when controlling for exit_reason.

**H2 — The apparent signal is an exit-mechanic artifact, not a WF property.** 

Winners are disproportionately `tp`/`trailing` (positions allowed to run) and losers disproportionately `stop`. If symbols that happen to trend also happen to score better in WF, the correlation is really "trending names do well live AND in WF" — a common-cause confound, not WF predicting live. If H2 holds, the WF-vs-`pnl_pct` correlation should **vanish within each exit_reason bucket**.

**H3 — The sign split is pure outlier luck.** 

The WF>0 bucket's +10.2% mean is carried by AXTI (+40%) and the WF≤0 bucket is dragged by several stops. With only 5 trades in the WF>0 bucket, one or two trades define the result. If H3 holds, the bucket separation should **not survive** dropping the top/bottom trade from each bucket, and the outlier-excluded correlation (+0.253) is the more honest estimate.

---

## Discriminating tests

**For H1** — re-run `scripts/analyze_wf_vs_live.py` at each trigger checkpoint and track the correlation trajectory. Persistence/tightening toward a stable positive value supports H1; regression toward 0 supports H3.

**For H2** — partial correlation controlling for exit_reason, or compute the WF-vs-`pnl_pct` correlation *within* the two largest buckets separately:

```sql
-- Run once n per bucket is large enough (~10+). Pseudo: join each live trade to
-- its point-in-time WF Sharpe (see analyze_wf_vs_live.build_matched), then:
--   corr(wf_sharpe_pit, pnl_pct)  WHERE exit_reason='stop'
--   corr(wf_sharpe_pit, pnl_pct)  WHERE exit_reason IN ('tp','trailing')
-- If both within-bucket correlations are ~0 while the pooled one is +0.35, H2 is supported.
```

**For H3** — already partially run: excluding AXTI+ASTS drops Pearson from +0.348 to +0.253 (signal survives but weakens). Extend by jackknife — drop each trade once, recompute, and report the correlation's min/max range. If the range straddles 0, the result is outlier-fragile.

---

## What we are NOT doing yet, and why

- **Not wiring WF Sharpe into the signal gate.** This is exactly the CLAUDE.md *"Consider walk-forward Sharpe in signal generation"* enhancement, which is itself gated on ≥2 weeks of Phase B live rows. The data here (n=23, not significant, outlier-driven, exit-confounded) does not yet justify a hard filter or a soft threshold penalty. A filter built on this would have vetoed the book's best trades (the "negative WF Sharpe is a poor veto" observation above is the direct counter-evidence).
- **Not declaring "WF predicts live."** The honest current statement is "weak positive rank tendency, not significant." Escalating beyond that requires more n and the H2 confound resolved.
- **Not re-pinning a regression test** the way `stop_bleed.md` pins baseline aggregates. n=23 is too small and too outlier-sensitive to make a stable canary; the re-runnable script is the durable artifact instead.

---

## Trigger to revisit

Re-run `python scripts/analyze_wf_vs_live.py` and append a status-log entry when **any** fires:

- Phase B reaches **≥40 `source='live'` closed trades** (first re-check; the 4 currently-open positions COHR/MRVL/WDC/USO exiting are the next +4). At ≥40, run the H2 within-bucket test for the first time.
- Phase B reaches **≥50 `source='live'` closed trades** (hard checkpoint — matches `stop_bleed.md`'s Phase-B threshold; run all three discriminating tests together).
- The correlation flips sign or moves by more than ±0.20 on any re-run — that instability is itself worth a log entry.

---

## Status log

**2026-06-01** — Observed. Surfaced from a chat exploration prompted by VRT showing a strong BUY (ensemble +0.814) against deeply negative WF folds. Built `scripts/analyze_wf_vs_live.py` for point-in-time matching. Headline at n=23: Pearson +0.348 (not significant, t=1.70), Spearman +0.393; naive (latest-run) matching gives +0.049, confirming the point-in-time bound is load-bearing. Two durable observations recorded: (1) negative WF Sharpe is a poor veto — 8/13 winners had negative pre-trade WF Sharpe; (2) counterexamples both ways (UAL +0.19 WF → worst loser; GLD −9.3 WF → winner). Three hypotheses ranked; only the outlier-jackknife portion of H3 run so far (excluding AXTI+ASTS → +0.253). No gate change made. Related findings: `stop_bleed.md` (the exit-mechanic confound named in H2), `tp_concentration.md` (winners cluster in tp/trailing).
