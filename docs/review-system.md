# Review system — how observations turn into fixes

This document maps the artifacts and skills that turn raw run output into a continually-evolving understanding of system performance. It is the answer to "where does this observation belong?" when reading a log or finishing a trade.

The technical tutorials in `01-system-overview.md` through `10-python-packages.md` cover *what the system does*. This doc covers *how we evaluate it*.

---

## Why this exists

The original goal: a way of continually evaluating performance, identifying issues, and analyzing what works and what doesn't. The system runs every weekday morning (`run_daily.bat`) and every Sunday (`run_weekly.bat`), and each run produces 600–15,000 lines of log output plus DB writes to `trade_log`, `signal_log`, `order_decisions`, `trailing_stop_log`, and friends. Without a structured review process, all of that is forgotten by the next run.

The review system is built around three observations:

1. **Different findings have different half-lives.** "Did the universe rotation we just saw stabilise next week?" is a one-week question. "Should we lower the trailing-stop activation threshold?" is a months-long question gated on accumulated evidence. They don't belong in the same file.
2. **Some findings warrant code, others warrant watching.** A confirmed bug needs a CLAUDE.md entry and a fix. A single-observation anomaly needs a gate that fires next run to confirm or refute it.
3. **A single trade outcome can teach more than a hundred summary statistics.** Individual case studies (AXTI's missed trail, SNOW's only-ever trail conversion, the 2026-05 losers) carry insight that a daily summary can't.

Three skills and four living documents fall out of those observations.

---

## The artifacts

| File | Half-life | Lives here when... |
|------|-----------|--------------------|
| `logs/daily/daily_run_YYYYMMDD.log` | One day (input) | Raw stdout/stderr from `run_daily.bat`. Source material, not a destination. |
| `logs/weekly/weekly_run_YYYYMMDD.log` | One week (input) | Raw stdout/stderr from `run_weekly.bat`. Source material, not a destination. |
| `logs/reviews/followups.md` | Days to ~4 weeks | A single-observation gate awaiting next-run confirmation. "Did X behave as expected in the *next* run?" |
| `CLAUDE.md` → *Outstanding bugs* / *Enhancements* | Until verified live | A confirmed bug with a fix in flight, or a sustained signal warranting a code change. "Did the fix hold up over multiple runs?" |
| `CHANGELOG.md` | Permanent | A CLAUDE.md fix that has been verified live in production. Terminal home — entries don't move out. |
| `docs/case_studies/*.md` | Permanent (living) | Deep post-mortem of a single trade or trade cluster. Updated when sister-doc triggers fire, not pruned. |
| `docs/findings/*.md` | Permanent (living) | A distributional pattern across many trades requires investigation before any action can be proposed. Status log appended over time; resolved findings stay in the folder for institutional memory. |
| `db/trading.db` (tables: `trade_log`, `signal_log`, `order_decisions`, `trailing_stop_log`, `walk_forward_results`, `universe_assets`, ...) | Permanent (append-only) | Source of truth. Every review and case study queries these. |

CLAUDE.md does more than host bug entries — it's also the project orientation doc (config reference, architectural decisions, ML model details). Only the *Outstanding bugs* and *Enhancements* sections participate in the review-system lifecycle.

---

## The skills

Three Claude Code skills under `.claude/skills/`. All three are user-invoked or auto-invoked from another skill; none run on a hook.

### `/daily-run-review`

Cadence: after each weekday `run_daily.bat`.

Reads: today's `logs/daily/daily_run_YYYYMMDD.log`, open items in `followups.md` (untagged only — skips `[weekly]`), `db/trading.db` for trade/signal context, existing case study `§6 Update triggers` lists.

Writes: appends new follow-ups to `followups.md`, moves resolved items to **Resolved**, surfaces escalated items in the review output with a copy-paste-ready CLAUDE.md proposal block. Does **not** auto-edit CLAUDE.md.

Side effects: may auto-invoke `/trade-case-study` when a hard trigger fires (|return| ≥ 20% on a closed trade, first-ever trailing-stop conversion, first-ever instance of newly-defined behavior). May surface a recommend-only trigger (loser cluster, no-exit position aging out, sister-doc trigger) that the user decides on.

Always ends with `.venv/Scripts/pytest tests/ -v` and reports `N/N passing`.

### `/weekly-run-review`

Cadence: after each Sunday `run_weekly.bat`.

Reads: this week's `logs/weekly/weekly_run_YYYYMMDD.log` (5–10× the size of a daily log), open items in `followups.md` (**both** untagged and `[weekly]`-tagged), prior weekly log for cross-run trend analysis, `db/trading.db`.

Writes: same as daily-run-review — `followups.md` mutations, copy-paste CLAUDE.md proposals. Also **prunes Resolved entries older than 30 days** and **prompts on Open items older than 4 weeks**. The pruner is the only thing that touches old followups.

Catches signals invisible to the daily skill: universe rotation magnitude, training duration regressions, BUY:SELL ratio trajectory across weeks, FinBERT coverage distribution, per-symbol average Sharpe drift, news-source mix.

### `/trade-case-study`

Cadence: rare. Auto-invoked from `/daily-run-review` on hard triggers; user-invoked via `/trade-case-study <SYMBOL>` or `/trade-case-study losers`.

Reads: `db/trading.db` exhaustively (`order_decisions`, `signal_log`, `trailing_stop_log`, `fundamental_data`, `news_cache`, `ohlcv_bars`, `signal_runner_log`, `universe_assets`), the relevant `logs/daily/*.log` files across the holding period, IBKR live state via `scripts/open_positions.py` / `scripts/open_orders.py`.

Writes: `docs/case_studies/<name>_<YYYY-MM>.md`. Surfaces CLAUDE.md proposals in the chat response (not in the file itself). Existing exemplars are the canonical structure reference — `axti_2026-04.md` (solo winner), `snow_2026-04.md` (still-open trail conversion, matched-pair to AXTI), `losers_2026-05.md` (group analysis).

Update model is "append, don't rewrite" — sister-doc cross-references and `§6 Update triggers` keep the docs living. If SNOW's trail eventually fires, that becomes a new section in `snow_2026-04.md`, not a new file.

Does **not** run the test suite — it writes documentation, not code.

---

## Lifecycle: how an observation flows

```
                         ┌─────────────────────────┐
                         │  raw log line / DB row  │
                         └────────────┬────────────┘
                                      │
                                      ▼
                         ┌─────────────────────────┐
                         │  /daily-run-review or   │
                         │  /weekly-run-review     │
                         └────────────┬────────────┘
                                      │
        ┌────────────────┬────────────┼────────────┬────────────────┐
        │                │            │            │                │
        ▼                ▼            ▼            ▼                │
single-observation  confirmed   hard trigger   distributional       │
gate (e.g. "does    bug or      fired (closed  pattern across       │
X stabilise next    sustained   trade ≥ 20%,   many trades, needs   │
run?")              signal      first trail    investigation        │
                    requiring   conversion)    (human judgment;     │
                    code                       NOT auto-spawned)    │
        │                │            │            │                │
        ▼                ▼            ▼            ▼                │
┌──────────────┐ ┌──────────────┐ ┌─────────────┐ ┌──────────────┐  │
│ followups.md │ │ CLAUDE.md    │ │/trade-case- │ │docs/findings/│  │
│ → Open       │ │ → Outstand-  │ │ study       │ │ <name>.md    │  │
│              │ │   ing bugs   │ │ (auto-      │ │              │  │
│ Resolves     │ │   or Enhan-  │ │ invoked)    │ │ Status log   │  │
│ when: ...    │ │   cements    │ │             │ │ appended     │  │
│ Escalates    │ │              │ │ writes      │ │ over time as │  │
│ if: ...      │ │ Status: code │ │ docs/case_  │ │ investigation│  │
└──────┬───────┘ │ complete     │ │ studies/    │ │ progresses   │  │
       │         │ YYYY-MM-DD   │ │ <sym>_      │ │              │  │
       │         │ awaiting     │ │ <YYYY-MM>.md│ │ observed →   │  │
       │         │ live verif.  │ │             │ │ hypothesized │  │
       │         └──────┬───────┘ │ may surface │ │ → tested →   │  │
       │                │         │ own         │ │ resolved     │  │
       │                │         │ CLAUDE.md   │ └──────┬───────┘  │
       │                │         │ proposals   │        │          │
       │                │         └─────┬───────┘        │          │
       │                │               │                │          │
       ▼                ▼               ▼                ▼          ▼
 Resolves /      Verified live   Reviewed in      Terminal:    (anything
 escalates       in production   next review      escalate to  not matching
 (criterion      (run confirms   via §6 update    CLAUDE.md,   any branch
 met next        fix) → move     trigger →        cross-ref    is logged
 run) →          CLAUDE.md       updated in       from case    only — no
 pruned after    entry →         place (append-   study, or    artifact
 30 days by      CHANGELOG.md    only; lives      retire as    written)
 weekly skill    with Verified:  forever)         noise.
 OR propose      paragraph                        Resolved
 new             naming the                       findings
 CLAUDE.md       run                              STAY in
 Outstanding                                      folder
 bugs entry                                       (institutional
 via §8                                           memory)
```

The four terminal states are:

- **`followups.md` → Resolved → pruned** (short-lived observations that confirmed)
- **`CLAUDE.md` → `CHANGELOG.md`** (bugs that shipped and verified)
- **`docs/case_studies/*.md` → updated forever** (trade-level insight)
- **`docs/findings/*.md` → status log appended forever, terminal state recorded** (distributional patterns; resolved findings remain in the folder for institutional memory rather than being pruned)

The escalation arrow `followups → CLAUDE.md` is the most important one in this diagram: it's how single-observation watches mature into code changes when the *Escalates if* criterion fires. The findings → CLAUDE.md / case-study arrows are the second-most: they're how distributional diagnoses, once resolved, either escalate into specific code work (Outstanding bugs / Enhancements) or anchor to a specific trade narrative (case study sister-doc link).

---

## Boundary rules (where confusion usually lands)

### `followups.md` vs CLAUDE.md "Outstanding bugs"

| If the finding is... | It belongs in... |
|----------------------|------------------|
| "Watch whether X holds next run" | `followups.md` |
| "X is broken; here's the fix" | CLAUDE.md *Outstanding bugs* |
| "X is shipped but unverified live" | CLAUDE.md (with `*(code complete YYYY-MM-DD — awaiting live verification)*` marker) |
| "X needs more data before we decide" | CLAUDE.md *Enhancements* (with explicit *Trigger to revisit* line) |
| "X works as designed but is worth tracking long-term" | CLAUDE.md *Enhancements* or *Design notes* |

If a follow-up's *Escalates if* criterion fires, it typically becomes a new CLAUDE.md *Outstanding bugs* entry. The skill renders the proposal in fenced markdown so the user can paste it directly.

### Case study vs follow-up

| If the finding is... | It belongs in... |
|----------------------|------------------|
| Specific to one trade (or 3–5 related trades) | `docs/case_studies/<name>_<YYYY-MM>.md` |
| Universe-wide pattern across many trades | `followups.md` or CLAUDE.md depending on certainty |
| Single-bar anomaly | `followups.md` |
| "What happened with X?" with day-by-day mechanism worth reconstructing | Case study |

Case studies are also where matched-pair refinements happen — AXTI alone said "the trail can't engage on fast moves," but SNOW alongside AXTI refined that to "the trail's engagement depends on intraday-vs-overnight arrival sequence." That refinement could not have come from a follow-up entry.

### CLAUDE.md vs CHANGELOG.md

CLAUDE.md is where in-flight bug work lives. CHANGELOG.md is where it goes to die — the *Convention: documenting fixed bugs* section in CLAUDE.md spells out the retirement protocol, but the short version is:

1. Fix lands → entry gets `*(code complete YYYY-MM-DD — awaiting live verification)*` marker.
2. Production run confirms the fix behaves as designed → entry moves to CHANGELOG.md with a new `Verified:` paragraph naming the run.
3. Original problem description and implementation notes are preserved verbatim so future agents can grep for the file paths and function names.

CHANGELOG entries never come back to CLAUDE.md.

### Finding vs case study vs CLAUDE.md

| If the observation is... | It belongs in... |
|--------------------------|------------------|
| About one trade (or 3–5 related trades), reasoning from day-by-day mechanics | `docs/case_studies/<name>_<YYYY-MM>.md` |
| A distributional pattern across many trades, no fix in flight, hypotheses to test | `docs/findings/<name>.md` |
| An architectural or methodological *rule* (constant across the system, already diagnosed) | CLAUDE.md *Key Architectural Decisions* |
| A confirmed bug with code in flight or about to ship | CLAUDE.md *Outstanding bugs* |

The **unit of analysis** distinguishes finding from case study: a case study reasons from *specific named trades* (entry signals, day-by-day price action, exit mechanics, counterfactual knobs); a finding reasons from *aggregate distributions* (per-bucket averages, hypothesis discriminators, sample sizes).

**Diagnosed-ness** distinguishes finding from architectural decision: a finding is awaiting diagnosis (hypotheses ranked, tests specified, action gated); an architectural decision is the diagnosed rule itself. The fold-end / dedup-vs-raw / cost-asymmetry notes in CLAUDE.md belong there because they *are* the diagnosed rules surfaced during the Phase 3 benchmark-relative tracking work. "Stop-out exits underperform SPY" is a finding because the diagnosis hasn't happened.

See `docs/findings/README.md` for the full scope rule, document structure, and lifecycle.

---

## Cadence summary

| When | What runs | What gets written |
|------|-----------|-------------------|
| Mon–Fri 09:40 ET | `run_daily.bat` | `logs/daily/daily_run_YYYYMMDD.log`, DB writes to `signal_log` / `order_decisions` / `trailing_stop_log` / `signal_runner_log` / `equity_snapshots` |
| After each daily run | `/daily-run-review` (user-invoked) | `followups.md` mutations, may auto-invoke `/trade-case-study`, CLAUDE.md proposals in chat |
| Sunday 01:00 ET | `run_weekly.bat` | `logs/weekly/weekly_run_YYYYMMDD.log`, DB writes to `walk_forward_results` / `trade_log` / `universe_assets` |
| After each weekly run | `/weekly-run-review` (user-invoked) | Same as daily + prunes Resolved >30 days + prompts on Open >4 weeks |
| Hard trigger fires inside a review | `/trade-case-study <SYMBOL>` (auto-invoked) | New or appended `docs/case_studies/*.md` file |
| Anytime | `/trade-case-study <SYMBOL>` (user-invoked) | Same — bypasses §0 trigger evaluation |
| When a CLAUDE.md fix verifies live | (manual move) | Entry migrates from CLAUDE.md to CHANGELOG.md |

---

## How this evolved

The structure above wasn't designed up front. Each piece was added in response to a specific friction:

1. **Raw logs first.** `run_daily.bat` / `run_weekly.bat` wrote to `logs/daily/` and `logs/weekly/`. Reading them after the fact was the only review.
2. **CLAUDE.md outstanding bugs.** Logs surfaced bugs faster than they could be fixed. CLAUDE.md became the backlog.
3. **`*(code complete — awaiting live verification)*` markers.** Bugs were getting fixed and then forgotten before anyone confirmed the fix actually worked in production. The marker convention forced an explicit "did this hold up?" gate.
4. **CHANGELOG.md retirement.** CLAUDE.md was getting long. Verified-live entries needed a terminal home where they wouldn't clutter the in-flight backlog but also wouldn't get lost — `git log` isn't searchable by symptom.
5. **`followups.md`.** Some observations weren't bugs yet — they were "watch whether this happens again." Putting them in CLAUDE.md polluted the bug list; leaving them in the log meant they were forgotten. The format `Resolves when: X. Escalates if: Y.` came from the need to make the next-run review *deterministic* rather than judgment-driven.
6. **`/daily-run-review` skill.** Manually reading the log every day was tedious and inconsistent. The skill codified what to look for and what to write.
7. **`/weekly-run-review` skill.** Weekly logs are 10× the size and surface signals (universe rotation, Sharpe distribution, FinBERT coverage) that don't appear daily. The daily skill couldn't reasonably handle weekly cadence, so the two split.
8. **`/trade-case-study` skill.** AXTI's near-miss trail conversion was the catalyst — a single trade contained enough mechanism to warrant deep reconstruction that wouldn't fit in a punch list. The skill formalised what a good case study looks like (day-by-day price table, skill-vs-luck retrospective, sister-doc cross-references, update triggers).
9. **§8 CLAUDE.md proposal format.** Early review outputs were paragraphs of "you should add this to CLAUDE.md." Rendering proposals as copy-paste-ready fenced blocks shortened the friction from minutes to seconds.
10. **Auto-invoke triggers in `trade-case-study` §0.** Some trades clearly warranted a case study (|return| ≥ 20%, first-ever trail conversion) and humans were the bottleneck on noticing. The hard/soft trigger split lets the daily skill auto-execute the deterministic cases and only ask permission for the judgment ones.
11. **Findings folder.** The Phase 3 benchmark-relative tracking work on Page 10 surfaced two patterns — stop-bleed (−7% avg excess across 526 raw stop-out trades) and TP-concentration (9 of 49 deduped trades carry the entire +124% cumulative excess) — that had no home in the existing structure. Neither was single-trade enough for a case study, fix-in-flight enough for an Outstanding bug, forward-looking enough for an Enhancement, or single-observation enough for a follow-up. They were *distributional diagnostic findings* — confirmed aggregate patterns awaiting hypothesis-driven investigation. The `docs/findings/` folder is the smallest structural addition that distinguishes that category from the four pre-existing ones; the boundary table in `findings/README.md` enforces the distinction.

Each layer was the smallest thing that solved a problem the previous layer had created. The result is a structure with real complexity but no redundant parts — every file and skill has a distinct role that the others can't fill.

---

## Quick reference

**"Where does this observation belong?"**

- One-line gate awaiting next run → `followups.md` Open section
- Fixable bug with code → CLAUDE.md *Outstanding bugs*
- Direction worth pursuing when evidence accumulates → CLAUDE.md *Enhancements* with a *Trigger to revisit*
- Single-trade mechanism worth reconstructing → `docs/case_studies/`
- Distributional pattern across many trades, no fix in flight, needs investigation → `docs/findings/<name>.md`
- Bug that verified live → migrate CLAUDE.md → CHANGELOG.md

**"Which skill do I run?"**

- After `run_daily.bat` → `/daily-run-review`
- After `run_weekly.bat` → `/weekly-run-review`
- After a noteworthy trade exits → usually the daily skill auto-invokes `/trade-case-study`; user can run it directly anytime
