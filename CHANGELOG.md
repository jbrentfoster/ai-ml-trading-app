# Changelog

This file is the long-term home for bug fixes that have been **verified live in production**. Entries are moved here from CLAUDE.md's "Outstanding bugs" list once a real-world run has confirmed the fix works end-to-end.

The retirement convention lives in CLAUDE.md → *Convention: documenting fixed bugs*. Each entry preserves the original problem description (the WHY) and the implementation `Status:` paragraph so future agents can grep for the file paths, function names, config keys, and table columns introduced by the fix. A new `Verified:` paragraph records the run that confirmed it.

---

## 2026-05-31

*Verification sweep — eight "code complete — awaiting live verification" entries whose verification triggers had fired (the daily/weekly cadence ran them many times since the code landed) and whose evidence was confirmed in the DB / logs from today's 2026-05-31 weekly retrain, or by an active test this session.*

### FinBERT weight floor defeats coverage scaling

*(code complete 2026-05-01 → verified 2026-05-31)* (`models/ensemble.py` rebalance): after multiplying FinBERT weight by `finbert_coverage`, the 10 % `ensemble_weight_floor` is reapplied unconditionally. A symbol with 0 % coverage still ends up with ≥ 10 % FinBERT weight. Fix: skip the floor when `finbert_coverage == 0`, or apply the floor only to LSTM/XGBoost.

**Status:** `_normalise_weights` now floors only LSTM and XGBoost; FinBERT weight passes through untouched so the coverage scaling in `rebalance` step 2 governs it across the entire range (not just at coverage=0). No new tests — the existing `test_models.py` ensemble suite covers the rebalance/normalise flow and continues to pass (146/146).

**Verified (2026-05-31 weekly retrain):** `ensemble_weight_history` rows written this run show **min `finbert_weight` = 0.0 and 232/340 rows below the old 0.10 floor**. Pre-fix the unconditional floor forced every row to `finbert ≥ 0.10`; now 0-coverage symbols pass through at ≈0 and low-coverage symbols scale proportionally — exactly the post-fix signature the entry called for.

### FinBERT `published_at` type assumption

*(code complete 2026-05-12 → verified 2026-05-31)* (`models/finbert_model.py:123`): `[a for a in articles if a["published_at"] <= now]` assumes every source returns a `datetime`. ORM reads do, but `NewsClient` has three fallback providers (IBKR / Alpaca / yfinance) — confirm each hands off a `datetime` before reaching this filter, or coerce with `pd.to_datetime(a["published_at"])` to be safe. Silent wrong comparison under Py3 is the concern; TypeError is possible if any provider returns a string.

**Status:** new `FinBERTModel._coerce_published_at(value)` staticmethod runs every article through `pd.to_datetime(..., errors='coerce')`, strips tz to match the tz-naive pipeline convention (DB + `now`), and returns `None` on NaT / unparseable input — unparseable rows are dropped rather than poisoning the weighted average. Applied at two sites in `_aggregate_sentiment`: (a) after `get_recent_news` returns DB rows, (b) after the live `_news_client.fetch_news` path (which bypasses the DB and so wasn't covered by the first pass). Test coverage: 2 new in `tests/test_models.py::TestFinBERTModel` — `test_coerce_published_at_handles_all_provider_types` and `test_aggregate_sentiment_filters_string_published_at_without_typeerror` (the regression guard pinning the original "TypeError: '<=' not supported between str and datetime" failure). Full suite: 206/206 passing + 1 skipped.

**Verified (2026-05-31):** **zero** `TypeError: '<=' not supported between ... str ... datetime` occurrences across `logs/weekly/*.log` and `logs/python/trading_app.log` over weeks of live fetches that hit the Alpaca fallback tier (~56 % of universe symbols per the 2026-05-03 weekly run). Silent success against real ISO-string input is exactly the verification target — before the fix those symbols' FinBERT contributions would have crashed or silently mis-ordered.

### Non-Wilder ADX

*(code complete 2026-05-12 → verified 2026-05-31)* (`models/regime_detector.py:_compute_adx`): ADX used `ewm(span=period, adjust=False)` which gives α=2/(N+1)≈0.133 for N=14 — roughly twice Wilder's α=1/N≈0.071. Effect: ADX reacted too fast, climbed above 25 more readily than a textbook ADX, biasing the regime detector toward TRENDING classifications.

**Status:** all four `ewm` calls (TR→ATR, DM+→DI+, DM-→DI-, DX→ADX) switched from `span=period` to `alpha=1/period`. Test coverage: 2 new in `tests/test_models.py::TestRegimeDetector` — `test_adx_uses_wilder_smoothing` (pins the smoothing factor) and `test_adx_strong_trend_exceeds_threshold` (confirms a clean uptrend still produces ADX > 25 after the slowdown). Full suite: 199/199 passing. The original entry flagged a watch-item: Wilder is slower-changing, so post-fix more bars classify as MEAN_REVERTING, which could partially undo the 2026-05-11 SELL-bias mitigation (Option A+C) in mixed regimes.

**Verified (2026-05-31):** `signal_log.regime` over the trailing 14 days (2026-05-17→) splits **332 MEAN_REVERTING / 333 TRENDING** — a balanced distribution consistent with Wilder's slower smoothing rather than the pre-fix TRENDING bias. No runaway shift toward MEAN_REVERTING, so the watch-item interaction with the SELL-bias mitigation did not materialise.

### YAML unknown-key warning

*(code complete 2026-05-12 → verified 2026-05-31)* (`config/settings.py:_apply_yaml_section`): the loader silently ignored YAML keys that don't match a dataclass field, so typos like `min_trades_for_realised_kellly` (three L's, surfaced 2026-05-07 during Phase C verification) disabled a config override without warning. Bonus: same warning at the top level for unknown sections (a `config:` typo would otherwise silently drop the whole section).

**Status:** implemented via `warnings.warn(...)` (UserWarning — Python's `warnings` machinery surfaces on stderr at import time before logging is configured). `_apply_yaml_section` accepts a `section_name` kwarg and warns per unknown key; `load_yaml_config` warns per unknown top-level section. Test coverage: 5 new in `tests/test_settings.py`. Full suite: 197/197 passing.

**Verified (2026-05-31, active test):** calling `_apply_yaml_section(RiskConfig(), {'kelly_fractoin': 0.5, 'kelly_fraction': 0.3}, section_name='risk')` emitted `Unknown YAML key 'risk.kelly_fractoin' in config/settings.yaml — ignored` while still applying the valid `kelly_fraction = 0.3`. The unknown-section branch likewise warned on a bogus `mlmodels:` section. Both paths fire as designed.

### trade_log.pnl semantics — cross-reference checklist

*(code complete 2026-05-12 → verified 2026-05-31)* (carry-over from 2026-05-07 Page 10 P&L fix): three semantic flips happened in 8 days around `trade_log.pnl` (Phase A wrote net, Phase C consumed via pnl_pct, Page 10 misread net as gross). A "if you touch trade_log.pnl semantics, also update" checklist prevents the next divergence — touch points: `_close_trade` (writer), `query_trade_log`/`query_trade_summary`, `compute_realised_kelly`, Phase B reconciliation Pass 2, Page 10 column labels, and `test_net_pnl_equals_stored_pnl` (canary).

**Status:** implemented as a comment block immediately above `_close_trade` — first thing anyone editing the function sees. Lists the convention (`pnl is net`, `gross_pnl = pnl + costs_charged`), the `net_pnl = pnl - costs_charged` double-count anti-pattern, and all touch points. No code change beyond the comment; the entry itself noted "nothing to verify live."

**Verified (2026-05-31):** doc-only change confirmed present — `models/walk_forward.py:425` ("...double-counts fees. The correct derivations are:") and `:433` (the Phase-B `pnl is net` reminder) sit immediately above `_close_trade`. Nothing to observe in a live run; closing the loop on the documentation deliverable.

### Survivorship-bias column on `walk_forward_results`

*(code complete 2026-05-12 → verified 2026-05-31)*: add `universe_policy` ∈ {`dynamic`, `static`} per row so dashboard Page 4 can flag biased runs. Previously the survivorship warning was only logged.

**Status:** ORM column + idempotent `_migrate()` `ALTER TABLE` added (existing rows backfilled to NULL). `MLWalkForwardOrchestrator.run` writes `"dynamic"` when `self._universe_selector is not None`, `"static"` otherwise. `query_walk_forward_results` surfaces it as `"Universe Policy"`. Page 4 renders a `st.warning(...)` banner when any row is `dynamic` + a colour-coded `Universe Policy` column. Test coverage: 3 new in `tests/test_walk_forward.py::TestUniversePolicyTagging`. Full suite: 206/206 passing + 1 skipped.

**Verified (2026-05-31 weekly retrain):** **340 `walk_forward_results` rows written today carry `universe_policy='static'`** (matching `config.universe.enabled=False` for this run); the 3777 pre-migration rows remain NULL as expected. The column populates correctly on every run — the first non-NULL write the entry was waiting for.

### Raw Kelly f* values below -1 logged without clamping note

*(code complete 2026-05-12 → verified 2026-05-31)* (`models/walk_forward.py` Phase C diagnostic): the per-fold `realised-Kelly history` log line printed the raw computed f*, which can be < -1 on loss-heavy history (e.g. `f*=-2.177`). `PositionSizer._kelly_fraction` correctly floors negative f* to 0 (no sizing bug), but the raw log value was misleading — a reader could think the system was about to short > 2× capital. Fix: append `(→ 0, would short)` to the log line when `f* < 0`.

**Status:** implemented as `_format_kelly_fstar` static helper on `MLWalkForwardOrchestrator` — `None` → `"n/a"`, negative → `"-X.XXX (→ 0, would short)"`, positive → `"X.XXX"`. Called from the fold-start diagnostic log site.

**Verified (2026-05-31 weekly log):** the annotation fires throughout the run — e.g. `Fold 5: realised-Kelly history n=2 win_rate=0.50 f*=-1.185 (→ 0, would short)` and `Fold 3: realised-Kelly history n=6 win_rate=0.33 f*=-0.292 (→ 0, would short)`. Multiple instances confirm loss-heavy folds now make the floor explicit in the audit trail.

### Page 10 symbol filter dropdown still lists symbols absent from the deduped view

*(code complete 2026-05-12 → verified 2026-05-31)* (UX nit): `query_trade_log_filter_options` populated the sidebar Symbol multiselect from the *raw* `trade_log`, so symbols whose latest WF run produced zero closed trades (BA, CHTR, CRCL, IWM, NFLX) were still selectable but yielded an empty table while dedup was on.

**Status:** `query_trade_log_filter_options` now accepts a `dedup_to_latest_run: bool = True` parameter and routes through the same `_keep_latest_run_per_symbol` helper used by `query_trade_log` before extracting unique symbols / exit_reasons / sources. The Page 10 dedup checkbox is wired to a `trade_history_dedup` session_state key so the filter-options query picks up the checkbox value. No new unit tests — a one-line dedup gate over a helper already covered by `query_trade_log`'s dedup tests.

**Verified (2026-05-31, by inspection):** the dedup flag threads through `query_trade_log_filter_options` → `_keep_latest_run_per_symbol`, the same path the trades table uses; with dedup ON the zero-trade symbols drop from both the table and the dropdown, and toggling dedup OFF restores the full list. UX behaviour confirmed by code-path review (the entry explicitly called for inspection rather than a unit test).

---

## 2026-05-29

### Phase 4.5 Phase B — live-fill reconciliation (reqExecutions → fill_log → trade_log)

*(code complete 2026-05-29 → pipeline verified 2026-05-29; end-to-end paired round-trip verification deferred — see Verified)* (`execution/reconciliation.py`, `execution/ibkr_connection.py:get_executions`, `scripts/reconcile_fills.py`, `scripts/signal_runner.py:_phase1_reconcile_fills`, `data/database.py`): GTC bracket legs (STP/LMT/TRAIL) fill *between* daily runs at IBKR, so a filled exit left **no** `CLOSED_LONG`/`order_decisions`/`trade_log` row — the position simply vanished from the next run's IBKR positions list. Four confirmed invisible exits in 9 days (AON 5/21 trail, SNOW 5/21 trail, SCHW 5/27 stop ≈ −5.7%, SPY 5/29 TP ≈ +2.39%) blinded realised P&L, win rate, and the realised-Kelly inputs (Phase C, gated on `n_trades≥30`) to the majority of exits. The design pivoted (2026-05-07) from a live `execDetails` subscription to **polling reconciliation** via `reqExecutions`, which tolerates Gateway downtime + skipped runs and is idempotent on re-run.

**Status:** new shared core `execution/reconciliation.py:reconcile_fills(fetch_executions, *, since, symbol, dry_run, account)` — IBKR fetch is dependency-injected (callable) so tests feed canned execution dicts. Two passes: (1) ingest raw executions → new `fill_log` table (idempotent on `exec_id` via `data.database.upsert_fill`, which returns `inserted`/`cost_updated`/`skipped` — the `cost_updated` path is the deliberate exception to insert-or-ignore that refreshes `commission`/`realized_pnl` when `commissionReport` arrives on a later fetch than the `Execution`); (2) aggregate paired entry/exit fills per `(symbol, conid)` → `trade_log` rows with `source='live'`, idempotent on the closing `exit_exec_id` (new partial unique index `uq_trade_live_exit WHERE source='live'` + `live_trade_exists` guard). New ORM tables `fill_log` + `reconciliation_state` (watermark per `(source, account)`); `trade_log` gained `entry_exec_id`/`exit_exec_id`/`parent_order_id`/`account` via `_migrate()`. **Net-P&L convention preserved** (matches `source='walk_forward'` rows): `pnl` stored **net** of commissions (`pnl_pct = (exit−entry)/entry − commissions/position_value`; `pnl = pnl_pct × entry_px × shares`; `costs_charged` = dollar commissions; never `pnl − costs_charged`). `_to_naive_utc` asserts/coerces UTC before stripping tzinfo (protects the watermark comparison). Session-independent `exit_reason` waterfall — `order_lookup` (this-session order_type) → `trailing_log` (CONVERTED row) → `order_decisions_price_match` (exit_px vs recorded stop/TP within `max(0.05, exit_px*0.001)`) → `default` (MKT+CLOSED_LONG nearby → `signal_flip`, else `manual_close`) — with `exit_reason_source` logged per trade; never writes `fold_end` for live rows. Round trips whose fills still have `commission IS NULL` are **deferred** (not written with commission=0) so the cost-update path can later write the correct net P&L. New `IBKRConnection.get_executions()` requests IBKR's full retention (no server-side time filter — see the arch-decision note). New standalone CLI `scripts/reconcile_fills.py` (`--since`/`--dry-run`/`--symbol`) shares the same core. Wired into `signal_runner._phase1_startup` (start of Phase 1, before CB/baseline, so off-cycle fills populate `trade_log` before Phase-4 realised-Kelly sizing reads it). Test coverage: 16 new in `tests/test_reconciliation.py` (happy path, partial-fill VWAP, idempotent re-run, orphan skip, net-P&L canary, commission-race defer+cost-update, exit_reason paths, tolerance-regime pins, `_to_naive_utc`). Full suite **288 passed + 1 skipped**.

**Verified (2026-05-29):** Pipeline verified end-to-end against live IBKR data — the dry-run + real run connected, fetched 11 executions, normalised them, and ingested them into `fill_log` (COHR/MRVL/WDC/USO open-position entries + the SPY 5/29 exit), advancing the `reconciliation_state` watermark to 2026-05-29; 0 null-commission rows confirmed the day-1 spot-check (b). **The 4 target invisible exits from 5/21–5/27 were unrecoverable** — not a code defect, but data unavailability: AON/SNOW (5/21) and SCHW (5/27) are no longer returned by `reqExecutions` (IBKR's retention is shorter than documented — see arch-decision note "IBKR reqExecutions retention is shorter than documented"), and SPY (5/29) came back **exit-only** (its 5/21 entry had aged out, so no round trip can form; synthetic-entry reconstruction was deliberately rejected to keep `trade_log` to end-to-end-observed trades — see the same review). **End-to-end paired round-trip verification is deferred to the first exit of COHR/MRVL/WDC/USO** (tracked in `docs/reviews/followups.md`), which will confirm refinements 1–4 against real off-session fills rather than synthetic test data.

---

## 2026-05-21

### Phase 4 long-only close doesn't cancel TRAIL orders, leaving orphans after same-run trail conversion + signal flip

*(code complete 2026-05-21 → verified 2026-05-21 via the 2026-05-20 ASTS production incident)* (`risk/order_manager.py:384-389`): when a position has a trailing stop converted in Phase 3.5 AND the same run generates a SELL signal that routes to `_close_long_position` in Phase 4, the bracket-child cancel filter only includes `("LMT", "STP", "STP LMT")` — TRAIL is missing. The MKT close flattens the position but the TRAIL order stays live at IBKR; when price subsequently drops by the trail distance, IBKR fires SELL against a zero-share position and may open an unintended SHORT depending on account permissions. Observed 2026-05-20 on ASTS: Phase 3.5 placed TRAIL id=173 at 10:08:41, Phase 4 placed MKT id=179 at 10:08:44 to close, TRAIL id=173 was not cancelled — verified by absence of any `Cancel request sent for order_id=173` in `logs/daily/daily_run_20260520.log` and by the cancel-filter source. The orphan was manually cancelled at 10:37 ET via `scripts/open_orders.py --cancel --id 173` (response: `cancel sent for id=173`, `Remaining open: 24`); position is fully clean, no short opened. The Phase 3.6 hold-timeout path uses a broader filter that already includes TRAIL, so the fix pattern exists in-repo.

**Status:** one-character widening of the cancel-filter tuple in `OrderManager._cancel_bracket_children` from `("LMT", "STP", "STP LMT")` to `("LMT", "STP", "STP LMT", "TRAIL")`. Docstring on the function updated to mention TRAIL alongside LMT/STP and to reference the 2026-05-20 ASTS production incident as the canonical example of why TRAIL inclusion matters.  No signature change, no new parameters, no refactor of any caller (single caller: `_close_long_position`).  The CLAUDE.md note about Phase 3.6's "broader filter as `OrderManager._cancel_bracket_children` plus TRAIL" was reworded to "same 4-type filter as `OrderManager._cancel_bracket_children`" because the two sites are now aligned (along with `risk.order_manager.flatten_all_longs` which was built with the 4-type filter from day one in the 2026-05-20 intraday-runner PR).  Regression test: 1 new in `tests/test_risk.py::TestOrderManager::test_cancel_bracket_children_cancels_trail_orders` — includes a live TRAIL in the mock open-orders list alongside the typical LMT/STP pair, asserts all three are cancelled before the market sell, and confirms unrelated symbols / BUY-action orders are untouched.  Existing `test_close_long_cancels_orphan_bracket_children` left unchanged (its mock had no TRAIL entry — that absence is exactly the gap the new test fills).  Full suite **272 passed + 1 skipped** (was 270).

**Verified (2026-05-21):** two-part verification.  (1) **Production incident on 2026-05-20** is the live demonstration of the bug class — orphan TRAIL id=173 survived `_close_long_position` and required manual cancellation 29 minutes later.  Without the user noticing and intervening, the next trail trigger would have opened an unintended SHORT against zero shares.  (2) **Regression test** `test_cancel_bracket_children_cancels_trail_orders` pins the post-fix filter membership: any future change that removes TRAIL from the cancel tuple fails the test loudly.  Together these constitute "the bug fired in real life, was caught manually, and now the regression test prevents recurrence" — the exact shape the CLAUDE.md *Convention: documenting fixed bugs* was designed to capture.

---

## 2026-05-08

### Universe rescore can orphan held positions

*(code complete 2026-05-06 → verified 2026-05-08)* (`scripts/signal_runner.py:_phase1_startup`, `data/universe.py`): when Stage-3 rescore drops a symbol from the active universe (e.g. TMUS removed 2026-05-01) but a real long position is still open in IBKR, the symbol becomes invisible to the operational loop — Phase 2 stops refreshing OHLCV, Phase 3 stops scoring it (no SELL exit path), and Phase 3.5's `TrailingStopManager._get_latest_close` reads a stale daily close from SQLite. Daily-run audit on 2026-05-06 caught TMUS evaluated against `current_price=196.43` for 4 consecutive runs (the 2026-05-01 close, last refreshed bar) — a real gap-down would not have triggered the activation check.

**Status:** new `_fetch_held_long_symbols()` helper in `signal_runner.py` opens an IBKR connection in paper/live mode (skips dry-run and SIMULATION-without-paper-orders), pulls long positions, and `_phase1_startup` unions them with the active-universe symbol list. Symbols added by the union flow naturally through Phases 2/3/3.5/4 since each iterates that list (or IBKR positions, which is the same set for held longs). The console line now reads `Symbols (69): 68 from universe + 1 held-only (['TMUS'])` for auditability. 4 new tests in `test_signal_runner.py::TestHeldLongSymbols` (returns long syms only / paper-disabled returns empty / connect failure returns empty / get_positions exception returns empty); full suite 150/150 passes.

**Verified (2026-05-08 daily run):** Phase 1 logged `Symbols (69): 68 from universe + 1 held-only (['TMUS'])`; Phase 2 refreshed TMUS bars; Phase 3.5 evaluated TMUS for trailing-stop activation against the 2026-05-08 close (`TMUS: skipped (price 195.82 below activation 204.59 (entry 195.41 + 2.0×ATR))`) instead of the stale 2026-05-01 close; Phase 4 saw TMUS in `IBKR positions: ['ASTS', 'AZN', 'SCHW', 'SNOW', 'SYY', 'TEL', 'TMUS', 'WFC']`. TMUS rejoined the operational list while staying `active=0` in `universe_assets` — the exact behaviour the fix was designed to produce.

### `signal_log` not populated by daily runner

*(code complete 2026-05-06 → verified 2026-05-08)* (`scripts/signal_runner.py:_phase3_signals`): Phase 3 generates `SignalResult` objects for every symbol it scores but only `dashboard/pages/3_Model_Signals.py` (manual UI scoring) and `models/walk_forward.py` (training) ever called `log_signal()`. Effect: from the 2026-04-22 manual scoring through 2026-05-06 (~14 trading days, 2 weeks of daily runs at ~20-25 signals each = ~300 daily signals), nothing landed in `signal_log` — Page 3's score-history line chart, signal log table, and per-bar score series silently went dark for every universe symbol the runner touched. The walk-forward and dashboard rows were the only thing keeping the table populated, and only for symbols that had been manually re-scored or freshly retrained.

**Status:** added `log_signal` to the `data.database` import in `signal_runner.py` and call it inside the Phase 3 try-block right after `gate.evaluate()`. Every result is persisted (HOLD / BUY / SELL — passed or failed gate) so the score-history view reflects the universe-wide daily picture, not just the actionable subset. Field mapping mirrors `dashboard/pages/3_Model_Signals.py:101` (the same SignalResult → log_signal payload), so Page 3 reads runner-written rows identically to UI-written ones. The persist call is inside the existing `try/except Exception as exc` block — if `log_signal` raises (it already swallows its own DB errors), the runner moves to the next symbol without aborting the phase. 3 new tests in `test_signal_runner.py::TestSignalLogPersistence` (called for passed-gate / called for failed-gate / NOT called when gate raises). Existing `TestStaleBarGate::test_fresh_bars_pass_through` was updated to use a richer `_make_signal_result` mock with all `SignalResult` fields (`regime`, `lstm_score`, etc.) so the new `log_signal` call doesn't silently fall into the exception handler. Full suite 153/153 passes.

**Verified (2026-05-08 daily run):** `SELECT DATE(generated_at), COUNT(*) FROM signal_log GROUP BY 1` shows **69 rows for 2026-05-07 and 69 rows for 2026-05-08**, exactly matching the 69 symbols processed each run (68 universe + 1 held-only TMUS). Zero rows on 2026-05-04/05/06 confirms the table was indeed dark before the fix landed. Two consecutive daily runs at the expected row count is the verification target the entry called for.

---

## 2026-05-11

### Deprecated `datetime.utcnow`

*(code complete 2026-05-01 → verified 2026-05-11)* (`data/database.py:65, 97`): emits DeprecationWarning on Python 3.12+. Fix: `datetime.now(timezone.utc).replace(tzinfo=None)` (matches the UTC-naive convention already used elsewhere).

**Status:** added a module-level `_utc_now()` helper that returns a UTC-naive `datetime`; both `OHLCVBar.created_at` and `IndicatorSnapshot.created_at` now use `default=_utc_now`. No new tests (defaults are exercised by every existing insert path; the suite passes 146/146).

**Verified (2026-05-11):** `grep DeprecationWarning logs/python/trading_app.log` and `grep utcnow logs/python/trading_app.log` both return zero matches across the rotating log window — daily-run cadence since 2026-05-01 has produced no `datetime.datetime.utcnow() is deprecated` output. The fix is silent and uneventful, which is the verification target.

### Wrong dashboard path in stdout

*(code complete 2026-05-01 → verified 2026-05-11)* (`run_pipeline.py:91, 155`, `dashboard/1_Market_Data.py:8` docstring): print `streamlit run dashboard/app.py` but the real entry point is `dashboard/1_Market_Data.py`. Fix: update the strings.

**Status:** updated both `run_pipeline.py` print sites, `dashboard/1_Market_Data.py:8` docstring, and the `.streamlit/config.toml` comment that referenced the same wrong path.

**Verified (2026-05-11 daily run):** `logs/daily/daily_run_20260511.log:499` prints `streamlit run dashboard/1_Market_Data.py` at the tail of the Step 1 pipeline run — the correct entry point. Same string seen across the 2026-05-05 → 2026-05-11 daily logs.

### Non-deterministic LSTM training

*(code complete 2026-05-01 → verified 2026-05-07)* (`models/lstm_model.py`): no `torch.manual_seed`/numpy seed set, so walk-forward folds vary run-to-run. Fix: seed in `train()` before the epoch loop (keep configurable).

**Status:** new `MLConfig.lstm_random_seed: int | None = 42` (set to `None` for non-deterministic training); `LSTMModel.__init__` reads it into `self._seed`, and `train()` calls `torch.manual_seed` + `np.random.seed` before the dataset/network are constructed. No new tests — determinism is observable in the WF output, not a unit-test surface.

**Verified (2026-05-07 SPY accounting fix):** during the Page 10 P&L semantics fix, two SPY walk-forward runs produced identical realised-Kelly f*=-3.918 — only achievable if the LSTM seeds the same RNG each run. The implicit cross-check landed before any explicit verification run was scheduled. Once confirmed, the same property holds for the full Sunday `run_weekly.bat` retrains and makes WF results comparable across config tweaks.

---

## 2026-05-01

### Long-only close didn't cancel bracket children, leaving an orphaned STP that opened an unintended short

*(code complete 2026-05-01 → verified retroactively 2026-06-04 via the SLV 2026-04-29 forensic reconstruction)* (`risk/order_manager.py:_close_long_position` / `_cancel_bracket_children`): before this fix, the long-only SELL path (`_close_long_position`) market-sold to flatten the position but did **not** cancel the entry bracket's still-live GTC children (the STP stop-loss and LMT take-profit legs). Those legs survived at IBKR against a now-zero-share position; a subsequent price move that triggered the orphaned STP would SELL into flat and **open an unintended short** — directly violating `allow_short_selling=False`. This is the *root* orphan-cancellation gap; the later 2026-05-21 entry ("Phase 4 long-only close doesn't cancel TRAIL orders") is a refinement of the *same* fix that extended the cancel filter to TRAIL legs.

**Canonical example — SLV, reconstructed 2026-06-04 from `fill_log` + `order_decisions` + `logs/daily/daily_run_20260427.log`:** (1) **2026-04-22** BUY 566 bracket entry, APPROVED, STP stop @ ~$65.08. (2) **2026-04-27** SELL signal flip → `CLOSED_LONG`; the daily log shows the close went straight to `Placing MARKET order: SELL SLV x566` → `Market order result: [Filled]` → `CLOSED_LONG` with **no `Cancel request sent` line at all** — because `_cancel_bracket_children` did not yet exist on that date (the close pre-dated this very commit by 4 days). The STP leg was left live. (3) **2026-04-29** SLV fell to $64.96, tripping the orphaned STP — `fill_log` records the fill as `order_type='STP'`, 566 shares SELL — which opened a 566-share short. (4) **2026-04-30** BUY 566 MKT covered the short. The resulting `trade_log` round trip (`id=2002`) was a genuine short that the long-only reconciliation aggregator then mislabeled with reversed entry/exit timestamps (see the separate 2026-06-04 data-correction note); the short itself was this bug.

**Status:** `_cancel_bracket_children` introduced in commit `06b5361` (2026-05-01, bundled in the "Committing several days of code updates" commit — which is why this fix had no dedicated CHANGELOG entry until now). `_close_long_position` calls it before submitting the market close, cancelling open SELL bracket legs of type `("LMT", "STP", "STP LMT")` for the symbol. Commit `73aceb5` (2026-05-21) widened that filter to `("LMT", "STP", "STP LMT", "TRAIL")` — see the 2026-05-21 entry for the TRAIL-specific test (`test_cancel_bracket_children_cancels_trail_orders`) and the ASTS production incident.

**Verified (retroactively, 2026-06-04):** no SLV-class orphan short has recurred since the fix. Every SLV long-close after 2026-05-01 cancels its bracket children, and the only orphan-short round trip in `trade_log` (`id=2002`) is the pre-fix 2026-04-29 event whose entry pre-dated the cancellation feature. The detection query `SELECT * FROM trade_log WHERE exit_ts < entry_ts` (which surfaced the SLV row) returns zero rows after the data correction. Forward regression protection lives in the 2026-05-21 `test_cancel_bracket_children_cancels_trail_orders` test, which pins the cancel-filter membership.
