# LLM News Analyst (shadow workflow)

> **This is a research signal, not a trading signal.** Nothing in `signal_runner.py` reads the LLM analyst's output. It is surfaced only on **Page 11** of the dashboard. The design is deliberately decoupled from the live trading path so it can be iterated freely without risk to order generation. (Added 2026-06-02.)

---

## Why a second news model when FinBERT already exists?

FinBERT (see [doc 06](06-finbert-sentiment.md)) reads **headlines only** — `news_cache` never stored article bodies. A headline like *"Company Announces Restructuring"* is genuinely ambiguous: efficiency (bullish) or distress (bearish)? The disambiguating context lives in the body, which FinBERT never sees.

Two other limitations motivated a different approach:

1. **Attribution.** IBKR delivers news tagged to a *feed symbol*, but the article is frequently about a **different company**. A measured sample (2026-06-04) found feed-tagged news is actually about the feed symbol only **~36%** of the time — the rest are about suppliers, customers, competitors, or sector peers. FinBERT scores every article against its feed tag, so a Broadcom story tagged to NVDA contaminates NVDA's sentiment. (See [`findings/news_attribution_misallocation.md`](findings/news_attribution_misallocation.md).)
2. **Transparency.** FinBERT emits a single opaque number. There is no decomposition you can inspect, tune, or argue with.

The LLM analyst attacks all three: it reads **full bodies**, resolves **what the article is actually about**, and produces a **transparent, decomposable** score.

---

## What an LLM is good at — and what it isn't

The central design choice: **the model classifies; the code scores.**

A local large language model (an 8-billion-parameter model running on [Ollama](https://ollama.com)) is good at *reading comprehension* — judging direction, materiality, and novelty, and naming the company a story is about. It is **bad** at emitting a calibrated number like "+0.62" — that number is unstable across near-identical articles and impossible to audit.

So the LLM only ever returns structured fields. A deterministic Python function turns those fields into the score. This keeps the score **transparent** (the dashboard shows the decomposition), **tunable** (the formula is one function), and **stable** (the same fields always produce the same number).

```
Article body
     │
     ▼
LLM (Ollama, JSON mode)  ──►  structured extraction:
                              { direction, magnitude, novelty,
                                time_horizon, primary_entity,
                                entities[], event_type,
                                summary, rationale, confidence }
     │
     ▼
compute_composite_score()  ──►  one transparent number in [-1, 1]
```

---

## The two-step batch pipeline

The workflow is split into two **separate** steps, deliberately, so the slow part never blocks anything time-sensitive. Both are driven by `run_llm_news.bat` and both no-op unless `config.llm.enabled=True` (or `--force`).

### Step 1 — Body ingestion (`scripts/ingest_news_bodies.py`)
Back-fills `news_cache.body` with the full HTML-stripped article text via IBKR's `reqNewsArticle` API. **Needs IB Gateway**, but is fast (~seconds per article). Idempotent: `set_news_body` only writes when `body` is currently NULL, never overwriting. Piggybacks the morning window when the Gateway is already up.

### Step 2 — LLM scoring (`scripts/score_news_llm.py`)
Reads bodies from SQLite (**no Gateway needed**) and scores each un-scored body through Ollama, writing one row per `(symbol, article_id, model)` to `llm_news_analysis`. **Slow** — roughly **80 s/article** with the 8B model on the dev laptop (a 15 W ultrabook i5-1334U); `llama3.2:3b` is ~37 s if speed matters more than quality. Idempotent on the unique key.

Decoupling means the expensive scoring step never depends on Gateway uptime and can run on any cadence. It is kept **off the pre-market critical path** on purpose — a busy news day can take ~2 hours and must never delay `signal_runner`.

> This mirrors the project-wide [*batch files over persistent scheduler*](../README.md#automatic-scheduling-windows) decision: discrete steps that run and exit, not a long-lived daemon.

### Was the premise even true? (measured first)

Before building anything, two throwaway research scripts validated the assumptions:

- **`scripts/spike_body_availability.py`** measured that IBKR `reqNewsArticle` returns a usable full body for **~94%** of universe news — and, contrary to expectation, Dow Jones (`DJ-N`) bodies arrive **full** (median ~3,400 chars), not as paywall stubs.
- **`scripts/bench_llm_extraction.py`** measured throughput (the 80 s / 37 s numbers above) using Ollama's exact prefill/decode token counts. Extractions emit only ~100 output tokens (terse JSON), so a typical news day fits an overnight window.

Both scripts are kept only as the provenance of those numbers and can be deleted once the feature is stable.

---

## The composite score

`models/llm_analyst.py:compute_composite_score` turns three of the LLM's fields into a number:

```
sign      = +1 bullish  /  -1 bearish  /  0 neutral
intensity = magnitude / 5                          ∈ [0.2, 1.0]
nov_mult  = floor + (1 - floor) × (novelty / 5)    ∈ [floor, 1.0]

composite_score = clamp( sign × intensity × nov_mult,  -1,  +1 )
```

- **`magnitude` (1–5)** — how material the news is to the primary company's stock. Drives the magnitude of the score.
- **`novelty` (1–5)** — how new/surprising it is versus already-known. **Already-known news is discounted toward `floor`** (`config.llm.novelty_discount_floor`, default 0.5) because stale news is more likely already priced in. A genuinely new, highly material bullish story gets the full +1; the fifth reprint of it gets discounted.

The LLM *also* emits its own `[-1, 1]` guess (`llm_direct_score`), but that is stored only as a **shadow cross-check** — it never drives anything. The composite is the number the dashboard uses.

If `direction` or `magnitude` are unusable, the function returns `None` rather than guessing.

---

## Attribution — what is this article *actually* about?

Each extraction includes a `primary_entity` (a company **name**, e.g. "Marvell Technology"). The analyst resolves that name to a ticker **at read time** and classifies the article into one of four statuses:

| Status | Meaning | Example |
|--------|---------|---------|
| `matched` | The article really is about the feed symbol | NVDA-tagged story about Nvidia |
| `reattributed` | About a **different tracked** ticker — the value-add FinBERT misses | NVDA-tagged story about Marvell → MRVL |
| `untracked` | About a company we don't follow | story about HPE (not in universe) → `None` |
| `digest` | A multi-company roundup (see below) | *"Substantial Insider Sales: Morning Report"* |

Only `reattributed` and `untracked` count as "mismatches." A `matched` article whose feed tag also appears in a digest is **not** a mismatch.

**Why read-time resolution?** The name→ticker map is built from `universe_assets.name` (Marvell→MRVL, Broadcom→AVGO). Resolving at read time — the same convention as sector classification — means the heuristic can improve **without re-running the expensive 8B**. The stored `attributed_symbol` is advisory.

The matcher is deliberately **conservative**: exact ticker, or a token-set match against real company names — bare-ticker variants never fuzzy-match. (An earlier substring version false-matched "Nvi**dia**" → DIA.)

### Multi-company digests

Recurring wire roundups — *"Substantial Insider Sales: Morning Report"*, *"Comex Delivery Intentions"*, Barron's *"…and More Stocks That Explain Today's Market"* — enumerate dozens of companies. The 8B's single `primary_entity` pick from such a list is arbitrary, and the per-company sentiment is noise. Detection is **headline-based** (`_DIGEST_HEADLINE_PATTERNS`) because it is high-precision and **broadcast-safe**: the same digest arrives under many feed tags, and one headline rule reclassifies every copy. Digests resolve to `attributed_symbol=None`, render as `ticker=NA` on Page 11, and are dropped from the per-ticker drill-down. Extend the pattern list as new recurring formats surface.

---

## Event de-duplication

The same event is re-reported many times, and the 8B scores near-duplicates **inconsistently** — observed: four "Marvell surges" articles scored +0.00 / +0.90 / +0.00 / +0.90 on the same day. Left alone, re-reporting inflates a sentiment signal purely by volume.

`data/news_dedup.py` clusters scored articles into **events**, grouping by **`(resolved ticker, day)`** — *not* by text similarity:

```
event_score      = MEAN of all member reads      (every read counts)
representative   = highest-confidence member      (chosen only for display)
```

The **event score is the mean of every read** (so Marvell's event scores +0.45, not the +0.00 a single arbitrary representative happened to give). A representative article is picked only to decide which headline/summary to *show*.

> **Why not text-similarity clustering?** It was tried and **abandoned on real data**: intra-event Jaccard ran as low as 0.14 while a *different*-event pair (Marvell vs Broadcom) hit 0.19 — short reworded chip-sector headlines share too much generic vocabulary for any threshold to separate them. The resolved ticker is the reliable signal. The `jaccard`/`_tokens` helpers are retained for a possible future body-level pass.

**Known trade-off:** two genuinely different same-day stories about one company merge into one event. Far less harmful than re-report inflation, and the `event_size` + score spread keep it visible. Digests route to a `digest:<headline>` namespace so broadcast copies merge into one event instead of colliding with a real ticker's bucket.

---

## Where it lives — Page 11

The dashboard surface (`dashboard/pages/11_LLM_News_Analysis.py`) is **event-centric and table-first**:

- **Summary cards** — article/event counts, dedup ratio, attribution mix.
- **Events table** — one row per de-duplicated event: mean score, `Ticker[resolved]` vs `Feed[tag]` columns, Attribution status (match / re-attr / untracked / digest), with status/score/magnitude filters.
- **Symbol drill-down** — a daily sentiment time series plus per-event detail showing the underlying article reads.
- **Research expander** (collapsed) — score distribution, composite-vs-direct scatter, and per-run telemetry.

Read-time resolution (`data/ui_queries.py:query_llm_news_analysis`) means attribution and event clustering happen when the page loads — the stored rows are raw extractions, and the read layer applies the current name-map and dedup logic.

---

## Configuration

All under `config.llm` (Page 5 → settings; master switch defaults **off**):

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `False` | Master switch. Both scripts no-op unless True (or `--force`) |
| `model` | `llama3.1:8b` | Ollama tag. ~80 s/article on the dev laptop; `llama3.2:3b` ~37 s |
| `ollama_url` | `http://localhost:11434/api/generate` | Local Ollama HTTP endpoint (JSON mode) |
| `num_predict` | 300 | Max output tokens/article (real extractions emit ~100) |
| `request_timeout_s` | 1200 | Per-article hard cap |
| `min_body_chars` | 800 | Skip stub bodies below this (matches the body-availability "full" floor) |
| `lookback_days` | 3 | Body-ingest / scoring window |
| `novelty_discount_floor` | 0.5 | Composite-score novelty multiplier floor |

> `run_llm_news.bat` passes `--days 1` to the ingest step — the workflow is intended to run daily, so a one-day window is all that's needed each run.

---

## What's not done yet

The core shipped 2026-06-02; it is awaiting its first scheduled run and iteration. The notable open items (full list in CLAUDE.md → Enhancements → *LLM news analyst*):

- **Scheduling** — `run_llm_news.bat` is not yet wired into Windows Task Scheduler. Until then the feature runs only on manual `--force` invocation.
- **Event-score graduation** — mean is the transparent starting point; once enough events have been eyeballed, a "strongest corroborated read" combine may better capture materiality when a wire story is reprinted many times neutral.
- **Prompt sharpening** — score the *primary company's* move specifically, not the broader tape (some of the +0.00/+0.90 inconsistency came from articles framing a stock's surge against a down market).
- **`mentioned_tickers`** — resolve the full `entities` list, not just `primary_entity`, as secondary-relevance metadata.

---

## Relationship to the rest of the system

| | FinBERT (doc 06) | LLM news analyst (this doc) |
|--|------------------|------------------------------|
| Reads | Headlines | Full article bodies |
| Attribution | Feed tag (no resolution) | Resolved to the actual company |
| Output | One opaque score | Transparent decomposed composite |
| Speed | ms/headline | ~80 s/article |
| Consumed by | **`signal_runner` (live)** | **Page 11 only (shadow)** |

FinBERT remains the production sentiment input to the ensemble. The LLM analyst is a parallel research lane — if it proves out, the natural next step is wiring it as a **risk dial** (a sizing/threshold modifier) rather than a direct BUY/SELL source, and only once Phase B realised P&L can judge it.
