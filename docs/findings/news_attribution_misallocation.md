# News attribution misallocation — feed-tagged news is about the feed symbol only ~36% of the time

## Status

**observed** (2026-06-04) · **hypothesized** (2026-06-04) · untested

Validation query: `scripts/validate_news_scores.sql` (run weekly as events accumulate)

---

## Observation

The LLM news analyst's first full overnight run (Page 11, `llm_news_analysis`) gives the first *aggregate* measurement of a problem previously seen only in a hand-checked sample of 8 articles (the NVDA/AAPL→Marvell/Broadcom/Dell/HPE spot-check recorded under CLAUDE.md *"LLM news analyst is a shadow workflow"*). Across **235 de-duplicated events** (851 raw article-rows → 235 events, a 3.6× re-report compression), the four-way attribution split is:

| Attribution status | Count | Share | What it means |
|---|---|---|---|
| **matched** (about the feed symbol) | **84** | **35.7%** | The IBKR feed tag IS the article's primary subject |
| **reattributed** (about a *different tracked* ticker) | 32 | 13.6% | Tagged symbol A, primary subject is tradeable symbol B |
| **untracked** (primary subject not in universe) | 101 | 43.0% | Primary subject is a company we don't follow |
| **digest** (multi-company roundup) | 18 | 7.7% | No single subject — insider-sales / delivery-intentions roundups |

(`matched` is the residual: 235 − 32 − 101 − 18 = 84. Page-11 cards reported the other three directly as `Re-attr / Untrk / Digest = 32 / 101 / 18`.)

**The load-bearing number: the primary subject of a feed-tagged article is the feed symbol only 35.7% of the time.** FinBERT scores a headline's sentiment and attributes it to whatever symbol the IBKR feed tagged, with no notion of *who the article is about*. So FinBERT's per-symbol attribution is structurally correct for the 84 `matched` events and wrong (or, at best, scoring sentiment driven by another company's story) for the other 64%. The 32 `reattributed` events are the sharpest case: FinBERT credits a move to symbol A when the article is about tradeable symbol B — exactly inverting the signal for both names.

Event-score distribution over the same 235 events: **mean +0.18**, **Bull / Bear / Neutral = 124 / 56 / 55** (a mild bullish skew, consistent with the overbought tape diagnosis elsewhere in CLAUDE.md).

**Honesty caveat on the 64%.** `untracked` means the *primary* entity is untracked, not that the feed symbol is wholly irrelevant — the feed tag is sometimes a legitimate secondary mention (the NXPI insider-sales digest is the documented example). So 64% is the *mis-primary-attribution* rate, not a pure-error rate. Even granting the caveat, FinBERT is still scoring sentiment driven by a non-primary entity in those cases; the contamination holds, just with softer edges.

---

## Sample / dataset

- **Source**: `llm_news_analysis` on `db/trading.db` after the first full overnight scoring run, read 2026-06-04 via Page 11 (`data/ui_queries.py:query_llm_news_analysis` → `data/news_dedup.py:cluster_news_events`).
- **Model**: the 8B (`config.llm.model`, default `llama3.1:8b`). Single model; idempotent on `(symbol, article_id, model)`.
- **Event aggregation**: read-time. Events = (`attributed_symbol` resolved at read time, calendar day); event score = **mean** of all member `composite_score`s. Digests are routed to a `digest:<headline>` namespace and excluded from per-ticker buckets. See `data/news_dedup.py` and `models/llm_analyst.py:resolve_attribution_status`.
- **This is shadow data.** Nothing in `signal_runner` reads `llm_news_analysis`. The FinBERT comparison here is *structural* (FinBERT scores by feed tag, headline-only) — not a measured FinBERT-vs-LLM bake-off. No realised-outcome labels exist yet for either path.
- **No forward-return labels yet — and the events are clustered at the recent edge.** Smoke-running `validate_news_scores.sql` on 2026-06-04 returned **2 validatable events** at N=5, not because of a bar-coverage gap (all 68 attributed tickers have `ohlcv_bars`) but because the scored news is concentrated in the last three trading days: event-day distribution is 1 (05-21) / 1 (05-22) / 57 (06-01) / 56 (06-02) / 22 (06-03) across the 115 matched+reattributed candidate events. Only the two May events have a complete 5-bar forward window so far. This is the correct behavior (a 06-03 event cannot be validated on 06-04) and it quantifies exactly why "let it accumulate" is the right call — see the dated trigger below.

**Why this is a finding, not a bug or an enhancement.** It is a distributional pattern across many articles awaiting diagnosis, with no fix in flight. The tempting action — "remove FinBERT from signal generation" — is reasonable cleanup but is **not** a leak-fix: FinBERT is already near-weightless (its 30-day mean component score was **+0.01** in the 2026-05-11 bias diagnosis, vs XGBoost's −0.58 driving the book). Removing it changes almost nothing measurable. The actually-open question is whether the *LLM* signal carries predictive content that would justify wiring it in to replace FinBERT — and that requires the validation below, not a code change today.

---

## Hypotheses (ranked)

**H1 — The reattribution is genuine, tradeable value-add.** The 32 `reattributed` events identify a move in a *different tracked* ticker than the feed tag — information FinBERT structurally cannot extract. If H1 holds, reattributed events should predict the **reattributed** ticker's forward return at least as well as matched events predict the feed ticker's, and a sentiment signal built on resolved-ticker attribution would beat one built on feed-tag attribution. Falsifiable via the matched-vs-reattributed forward-return split in `validate_news_scores.sql`.

**H2 — The scores are too noisy to be predictive in any bucket.** The 8B scores near-duplicate reports inconsistently (the documented +0.00 / +0.90 / +0.00 / +0.90 Marvell spread; the prompt sometimes scores the *market* not the primary company — both are open items in the Page-11 enhancement list). If H2 holds, even `matched` events show a directional hit rate near 50% (no edge), and the attribution fix is moot because the underlying score is uninformative. This is the null hypothesis the validation must clear before any wiring is considered.

**H3 — The value is concentrated in the 14% reattributed slice; the 43% untracked is simply irrelevant.** Most feed-tagged news is about companies we don't trade, so neither FinBERT nor the LLM can extract universe-relevant signal from it — the entire usable signal lives in `matched` + `reattributed` (49% of events). If H3 holds, filtering to non-null resolved tickers (excluding untracked + digest) should *concentrate* whatever predictive signal exists, and per-event sentiment averaged over the raw feed (FinBERT's implicit behavior) is diluted ~2× by irrelevant news.

---

## Discriminating tests

**For H1 / H2 / H3 — the core validation** (`scripts/validate_news_scores.sql`): cluster scored articles into events, attach the resolved ticker's forward N-bar return (N=5 and N=21), and report, **bucketed by attribution status (matched vs reattributed) × score sign**:
- `n_events`, `avg_fwd_ret_pct`, and a `directional_hit_rate` (fraction of events where `sign(event_score) == sign(forward_return)`).

Read of the result:
- **H1 supported** if reattributed events show a directional hit rate comparably above 50% to matched events (the resolved-ticker attribution carries signal).
- **H2 supported** if *all* buckets sit near 50% hit rate / ~0 avg forward return (scores are noise regardless of attribution).
- **H3 supported** if matched+reattributed buckets show edge while a feed-tag-only baseline (scoring by `symbol` rather than `attributed_symbol`) washes out.

The SQL operates on stored fields (`attributed_symbol`, `composite_score`) and can cleanly separate **matched vs reattributed** (both have non-null `attributed_symbol`). It **cannot** cleanly separate `untracked` from `digest` (both store `attributed_symbol = NULL`), so it filters them out together — acceptable, since neither is universe-tradeable. A faithful 4-way split and proper Pearson/Spearman correlation need the read-time Python path (`resolve_attribution_status` + `cluster_news_events`), which is the natural next artifact (`scripts/analyze_news_scores.py`, mirroring `scripts/analyze_wf_vs_live.py`) once enough events have a forward window.

---

## What we are NOT doing yet, and why

- **Not removing FinBERT from the ensemble yet.** It is already near-weightless (+0.01 mean component, coverage-scaled toward zero) so removal is cosmetic, not corrective — and doing it now would conflate "honest accounting" with "fixing the signal," obscuring that XGBoost's overbought-tape behavior is the real driver. If we remove it, frame it as dead-weight removal with no expected behavior change, not a leak fix.
- **Not wiring the LLM signal into `signal_runner`.** Zero realised-outcome validation exists. Wiring an unvalidated signal in to replace a near-zero one is lateral motion with new risk. The validation query must first show H1/H3 over H2.
- **Not graduating the event score from mean → "strongest corroborated read"**, **not sharpening the extraction prompt**, **not resolving `mentioned_tickers`** — all three are open Page-11 enhancement items that would change the scores under analysis. Changing the scorer before measuring the current scorer against outcomes destroys the baseline. Measure first.
- **Not treating the 64% as a hard error rate.** The untracked secondary-mention caveat (above) means the honest claim is "mis-primary-attribution," and the validation must not over-credit the reattribution edge without the directional-hit evidence.

---

## Trigger to revisit / verification gate

Run `scripts/validate_news_scores.sql` and append a status-log entry when **any** fires. The binding constraint is the **forward window**, not event count — the current 135-event batch already exists; it just needs bars to accrue *after* the clustered 06-01→06-03 event days.

- **N=5 verdict on the current batch: ~2026-06-10** (5 trading days after the 06-03 cluster). At that point the bulk of the 115 matched+reattributed events become validatable and the matched-vs-reattributed hit-rate split (Result 2) has real n. This is the first real read and needs *no* new scoring runs — the labels arrive as the calendar advances.
- **N=21 verdict: ~2026-07-02** (21 trading days after 06-03). Edit the `+ 5` literal to `+ 21` and re-run.
- **Schedule `run_llm_news.bat`** (open Page-11 item #1) so *new* events keep landing — otherwise the analysis is forever stuck validating this one batch. Without scheduling, the N=5 read on 06-10 is a one-shot, not a trend. Scheduling is what turns "let it accumulate" into accumulation.
- **Phase B live rows accumulate** — once realised P&L exists for held names, the LLM event score can be tested against actual trade outcomes (not just forward price), the strongest possible label. Cross-gate with `wf_vs_live_correlation.md`'s Phase-B thresholds.

Escalation paths once the validation runs: H1/H3 supported → CLAUDE.md *Enhancements* entry proposing a gated, low-weight LLM sentiment slot keyed on resolved ticker. H2 supported → retire the signal-generation ambition; keep Page 11 as a research/attribution surface only, and proceed with FinBERT dead-weight removal.

---

## Status log

**2026-06-04** — Observed. First full overnight `score_news_llm.py` run surfaced the aggregate attribution split on Page 11 (235 events: 84 matched / 32 reattributed / 101 untracked / 18 digest; mean event score +0.18; Bull/Bear/Neut 124/56/55; dedup 851→235). Quantifies at scale the attribution flaw previously seen in an 8-article hand-check. Three hypotheses ranked; the central discriminator is whether the LLM scores predict the *resolved* ticker's forward return (H1) or are too noisy to predict anything (H2), with H3 (signal concentrated in the 49% tradeable-subject slice) as the structural corollary. Drafted `scripts/validate_news_scores.sql` as the accumulation-friendly discriminator. No code change to the signal path; explicit decision recorded NOT to remove FinBERT as a "fix" (it is already +0.01-mean near-weightless per the 2026-05-11 diagnosis) and NOT to wire the LLM signal in without forward-return validation. Related: CLAUDE.md *"LLM news analyst is a shadow workflow"* (design + the originating 8-article spot-check), the Page-11 enhancement open-items list (scheduling, mean→corroborated-read, prompt sharpening), and `wf_vs_live_correlation.md` (shares the Phase-B realised-outcome gate).

**2026-06-04 (same day) — validation query smoke-tested.** Ran `validate_news_scores.sql` against `db/trading.db`: SQL is correct, returns 2 validatable events at N=5. The near-empty result is *expected* — scored news clusters at the recent edge (event-days 1/1/57/56/22 across 05-21/05-22/06-01/06-02/06-03), so only the two May events have a 5-bar forward window yet; bar coverage is complete (68/68 attributed tickers in `ohlcv_bars`). Recorded the concrete verdict dates in the trigger section (N=5 ≈ 2026-06-10, N=21 ≈ 2026-07-02). No verdict on H1/H2/H3 yet — the labels arrive as the calendar advances; the action item is to re-run on 06-10.
