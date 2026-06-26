# Findings — distributional diagnostic observations

This folder holds documents about *distributional patterns* across many trades — confirmed aggregate observations that need diagnosis before any code, parameter, or signal-layer change can be proposed.

A finding is not a bug ("here's the fix"), not an enhancement ("here's the direction"), not a follow-up ("did this hold up next run?"), and not a case study ("what happened with this trade?"). It is a *pattern that needs investigation*. Hypotheses get ranked, discriminating tests get specified, but the work is not started — findings document what we know and what we'd need to know next, not the answer.

The category exists because the other artifacts in `docs/review-system.md` each have a load-bearing constraint that a distributional pattern doesn't satisfy. Case studies need a specific trade as the unit of analysis. Outstanding bugs need a fix in flight. Enhancements need a forward-looking direction with a trigger condition. Followups need a next-run gate. An observation like "stop-out exits underperform SPY by –7% across 526 trades" has none of those — it is an *observation about the aggregate* awaiting diagnosis.

---

## Scope rule

| If the observation is... | It belongs in... |
|--------------------------|------------------|
| About one trade (or 3–5 related trades), with day-by-day mechanism | `docs/case_studies/<name>_<YYYY-MM>.md` |
| A distributional pattern across many trades, no fix in flight, hypotheses to test | `docs/findings/<name>.md` |
| An architectural or methodological *rule* (constant across the system, already diagnosed) | CLAUDE.md *Key Architectural Decisions* |
| A confirmed bug with a fix in flight or about to ship | CLAUDE.md *Outstanding bugs* |
| A direction worth pursuing once evidence accumulates | CLAUDE.md *Enhancements* with a *Trigger to revisit* |
| A "watch whether X holds next run" gate | `docs/reviews/followups.md` |

The boundary between *finding* and *case study* is the **unit of analysis**: a case study reasons from *specific named trades* (entry signals, day-by-day price action, exit mechanics, counterfactual knobs); a finding reasons from *aggregate distributions* (per-bucket averages, hypothesis discriminators, sample sizes).

The boundary between *finding* and *architectural decision* is **diagnosed-ness**: a finding is awaiting diagnosis (hypotheses ranked, tests specified, action gated); an architectural decision is the diagnosed rule itself. The fold-end / dedup-vs-raw / cost-asymmetry notes in CLAUDE.md belong there because they *are* the diagnosed rules surfaced during Phase 3 of the benchmark-relative tracking work. "Stop-out exits underperform SPY" is a finding because the diagnosis hasn't happened.

---

## Document structure

Every finding has these sections, in this order:

1. **Status** — one of `observed` / `hypothesized` / `tested` / `resolved`. Date-stamped on every change.
2. **Observation** — the specific quantitative pattern, with the SQL or methodology that surfaced it. Numbers, not adjectives.
3. **Sample / dataset** — which view (deduped or raw), which date range, how many trades. Critical because findings shift as data accumulates.
4. **Hypotheses (ranked)** — what could explain it. Multiple, ranked by current plausibility. Each hypothesis must be falsifiable.
5. **Discriminating tests** — concrete SQL or analyses that would rule each hypothesis in or out. One test per hypothesis (minimum).
6. **What we are NOT doing yet, and why** — explicit. Prevents premature fixes. The most important section in the document.
7. **Trigger to revisit / verification gate** — when do we look at this again? Specific condition (sample-size threshold, time-window, downstream event).
8. **Status log** — dated entries appended over time as investigation progresses. Append-only; never rewritten.

The Status log is the active surface of the document. Every time hypothesis evidence accumulates, a discriminating test runs, or a trigger fires, an entry gets appended with a date and a short summary of what changed. Older entries stay verbatim. A future reader should be able to reconstruct the investigation's chronology by reading the log top to bottom.

---

## Lifecycle

Findings move through four states:

- **observed** — pattern noticed, no investigation yet.
- **hypothesized** — candidate explanations recorded, discriminating tests specified.
- **tested** — one or more discriminating tests run, evidence accumulating for or against hypotheses.
- **resolved** — a hypothesis is supported strongly enough to act on, or all hypotheses are ruled out and the pattern is reclassified as noise.

A resolved finding has four possible terminal states:

1. **Escalate to CLAUDE.md *Outstanding bugs*** — if the resolution reveals a specific code fix.
2. **Move to CLAUDE.md *Enhancements*** — if the resolution reveals a tunable knob worth pursuing once more evidence accumulates.
3. **Cross-reference from a case study** — if a single trade ends up exemplifying the diagnosed mechanism, the case study gains a sister-doc link to the finding and the finding's status log notes the cross-reference.
4. **Retire with a note** — if discriminating tests showed the pattern was noise after all (sample-size artifact, survivorship, etc.).

Resolved findings stay in this folder as institutional memory. They do not get pruned like `followups.md` Resolved entries. A reader six months from now should be able to find a finding that was investigated and rejected and understand *why* it was rejected, without re-deriving the analysis.

---

## How findings interact with the review skills

`/daily-run-review` and `/weekly-run-review` should:

- **Reference open findings** when relevant data appears in a run. Example: if today's run produces five new stop-out trades, the daily review should note that `stop_bleed.md` gained five new datapoints worth examining at the next investigation tick.
- **May append to a finding's Status log** when a discriminating test fires, a trigger condition is met, or a new bucket of evidence accumulates. The skill writes the entry; the human reads it.

Findings are **not auto-created by the skills**. This is the load-bearing difference from `followups.md`, which the skills mechanically append to whenever a single-observation gate seems worth watching. Findings require explicit human judgment that a pattern is *distributional and diagnostic*, not just a single-run anomaly. If the skill is unsure whether something is a follow-up or a finding, the answer is follow-up — followups have a one-month half-life, findings live forever.

---

## Current findings

| Document | Status | Opened |
|----------|--------|--------|
| [`stop_bleed.md`](stop_bleed.md) | observed, hypothesized | 2026-05-19 |
| [`tp_concentration.md`](tp_concentration.md) | observed, hypothesized | 2026-05-19 |
| [`wf_vs_live_correlation.md`](wf_vs_live_correlation.md) | observed, hypothesized | 2026-06-01 |
| [`news_attribution_misallocation.md`](news_attribution_misallocation.md) | observed, hypothesized | 2026-06-04 |
| [`entry_conviction.md`](entry_conviction.md) | hypothesized | 2026-06-10 |
| [`trail_vs_tp_capture.md`](trail_vs_tp_capture.md) | observed, hypothesized | 2026-06-16 |
| [`volatility_cohort_edge.md`](volatility_cohort_edge.md) | observed, hypothesized, tested | 2026-06-26 |
