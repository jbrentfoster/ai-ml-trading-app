# Changelog

This file is the long-term home for bug fixes that have been **verified live in production**. Entries are moved here from CLAUDE.md's "Outstanding bugs" list once a real-world run has confirmed the fix works end-to-end.

The retirement convention lives in CLAUDE.md → *Convention: documenting fixed bugs*. Each entry preserves the original problem description (the WHY) and the implementation `Status:` paragraph so future agents can grep for the file paths, function names, config keys, and table columns introduced by the fix. A new `Verified:` paragraph records the run that confirmed it.

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
