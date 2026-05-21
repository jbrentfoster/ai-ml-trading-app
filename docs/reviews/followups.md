# Review follow-ups

Cross-review continuity for `/daily-run-review` and `/weekly-run-review`. Each skill reads the **Open** section at the start (resolve / escalate / progress) and appends to it at the end.

## Format

```
- [ ] **YYYY-MM-DD** <observation>. *Resolves when:* <criterion>. *Escalates if:* <criterion>. (from <log-filename>)
```

- Prefix with `**[weekly]**` for items that only the weekly skill can evaluate (e.g. Sharpe-distribution shifts, universe rotation, FinBERT coverage trends). The daily skill ignores these.
- For multi-run criteria (e.g. "over 5 daily runs"), append a `progress:` line under the bullet rather than rewriting the bullet each time:
  ```
  - [ ] **2026-05-17** ... *Resolves when:* X holds for 5 daily runs. ...
    - progress: 2026-05-18 ✓ (1/5), 2026-05-19 ✓ (2/5)
  ```
- When resolved, move to **Resolved** with `→ YYYY-MM-DD` and a one-line note.
- When escalated, surface in the review output prominently and propose a new CLAUDE.md "Outstanding bugs" entry (don't auto-edit CLAUDE.md).
- Weekly review prunes **Resolved** entries older than 30 days and prompts user about any **Open** item older than 4 weeks.

## Boundary with CLAUDE.md

| Lives here | Lives in CLAUDE.md |
|---|---|
| "Did X behave as expected in the *next* run?" — single-observation gate | "Did the fix we shipped hold up over multiple runs?" — sustained signal, tagged *code complete — awaiting live verification* |
| Short half-life (days to a few weeks) | Long half-life (until verified live, then CHANGELOG.md) |
| Observations awaiting next-run confirmation | Known bugs awaiting a fix, or fixes awaiting verification |

A follow-up that fires its *Escalates if* condition typically becomes a new CLAUDE.md "Outstanding bugs" entry.

---

## Open

- [ ] **2026-05-19** First `run_eod.bat` execution (scheduled 16:30 ET on weekdays) — spot-check that `scripts/refresh_recent_bars.py` actually corrects the stale-Low canaries from today's review. After the first EOD run completes, query `ohlcv_bars` for (TMUS 2026-05-15, UAL 2026-05-18, TEL 2026-05-18) and compare `low` against yfinance's reported daily low. Pre-fix DB values were TMUS $186.77 / UAL $93.79 / TEL $200.80; yfinance shows $185.10 / $91.36 / $197.90. *Resolves when:* all three DB Lows match yfinance to within $0.10 after the first EOD run. *Escalates if:* the run failed (no `logs/eod/eod_run_*.log` written), OR the symbols weren't in the refreshed union (check log for membership), OR the values still don't match — the overwrite path or symbol-union logic has a bug. (from manual fix shipped 2026-05-19; CLAUDE.md "Stale partial-day bars" entry)
  - progress: 2026-05-20 — `logs/eod/` is still empty; no EOD run logged. Most likely cause is Windows Task Scheduler not yet wired (the EOD wrapper is separate from `run_daily.bat`, which fires pre-market). Not treating as escalated yet — the *code* isn't broken, the *schedule* isn't wired. Manual one-off `run_eod.bat` invocation would verify the script path; otherwise this stays open until the user sets up the 16:30 ET trigger.


- [ ] **[weekly] 2026-05-17** Option A+C SELL-bias mitigation trajectory: this weekly's universe-wide BUY:SELL was 934:1910 ≈ 1:2.04, a dramatic improvement from the 1:28 baseline. CLAUDE.md says don't pull the Option B trigger on count alone — wait for Phase B realised P&L. But the trend itself is worth tracking week-over-week. *Resolves when:* 3 consecutive weekly retrains hold BUY:SELL between 1:1 and 1:5 (indicates A+C is stable, not over-correcting either way). *Escalates if:* ratio exceeds 1:5 OR falls below 1:1 (over-correction toward longs in a way that looks like bias-flip). (from weekly_run_20260517)
  - progress: 2026-05-17 ✓ (1/3, ratio 1:2.04, improved from 1:2.32 the prior weekly)
- [ ] **[weekly] 2026-05-17** Universe rotation magnitude: this weekly marked 48 assets inactive (up from 15 the prior week — +220%). Stage 3 ranker is momentum + ADV, so a strong semis/AI run dominates. Watch whether this is a one-time regime shift or persistent week-over-week instability. *Resolves when:* 3 consecutive weekly refreshes mark ≤30 inactive (i.e. the +220% spike is one-off). *Escalates if:* next 2 weeklies both mark ≥30 inactive — universe is structurally thrashing and Stage 3 ranker needs review (e.g. smoothing the momentum percentile across multiple lookback windows). (from weekly_run_20260517)
  - progress: 2026-05-17 — 48 inactive (above threshold; 0/3)
- [ ] **[weekly] 2026-05-17** Sharpe regression watch — WFC and EW each dropped > 2.0 vs prior weekly (WFC -0.157→-2.388, EW +0.063→-2.016). Both are rotated-out so not immediately actionable, but watch for pattern. *Resolves when:* next 3 weeklies show no rotated-in or fixture symbol with Δ > 2.0 vs prior. *Escalates if:* any *fixture* (SPY/QQQ/sector ETFs) shows Δ > 2.0 in a single weekly — fixtures stay in the active set, so a Sharpe regression there is consumed by trading decisions. (from weekly_run_20260517)

## Resolved (last 30 days)

- **2026-05-20** IBKR error 10089 ("Requested market data requires additional subscription — Delayed market data is available") is logged at ERROR by `IBKRConnection._on_error` for every symbol when the intraday runner calls `get_last_price()` without a real-time subscription. Observed 13× in a single Gateway-up dry-run of `scripts/intraday_check.py` on 2026-05-20. (from manual intraday verification 2026-05-20)
  → 2026-05-21: 10089 added to the informational set in `execution/ibkr_connection.py:_on_error`; regression test `tests/test_ibkr_connection.py::TestErrorHandling::test_on_error_10089_logs_at_debug_not_error` pins DEBUG-level routing. Bundled with the `_cancel_bracket_children` TRAIL-filter fix in the same PR. Next daily / intraday runs will confirm DEBUG-level log lines (currently ERROR-level for every evaluated symbol — 13×/run on the intraday cadence).

- **2026-05-20** Orphan TRAIL order id=173 (ASTS): Phase 3.5 placed the TRAIL at 10:08:41 and Phase 4 immediately CLOSED_LONG at 10:08:45, but `_cancel_bracket_children` filter excludes TRAIL so id=173 was left orphaned. (from daily_run_20260520; ASTS case study `docs/case_studies/asts_2026-04.md`)
  → 2026-05-20: User cancelled id=173 via `scripts/open_orders.py --cancel --id 173` at 10:37 ET (confirmed by `cancel sent for id=173` + `Remaining open: 24`). Position fully clean; no short opened.
  → 2026-05-21: Code fix shipped — `_cancel_bracket_children` filter expanded from 3-type to 4-type (TRAIL added).  Regression test `tests/test_risk.py::TestOrderManager::test_cancel_bracket_children_cancels_trail_orders` pins post-fix filter membership.  CLAUDE.md "Outstanding bugs" entry retired to CHANGELOG.md (2026-05-21 section).  The 2026-05-20 production incident + this PR's regression test together constitute "verified live" per the *Convention: documenting fixed bugs* lifecycle.

- **2026-05-17** Universe rotation: 48 of 68 active assets rotated out in Sunday's full refresh; the new universe is heavily semis/AI/crypto (NVDA, AMD, AVGO, MSTR, NBIS, IONQ, AAOI, etc.). Monday's daily needs to train ~48 new symbols via `train_models.py` (skip-existing). (from weekly_run_20260517)
  → 2026-05-19: 2 consecutive daily runs complete with no training failures. 2026-05-18 trained 16 / skipped 52 / failed 0 (52 min total). 2026-05-19 trained 0 / skipped 68 / failed 0 (~9 min total). Both well under the 3h budget; criterion satisfied.
- **2026-05-18** Trailing-stop near-miss watch: AON sits 22¢ below activation today (326.29 vs 326.51 = entry 312.67 + 2×ATR). Of 9 held positions, only SNOW has converted; AON is the next closest. (from daily_run_20260518)
  → 2026-05-19: AON CONVERTED at price 327.39 vs activation 326.51 (entry 312.67 + 2×ATR=326.51, trail=$13.70). Next-day close crossed activation by $0.88; squeeze-out failure mode ruled out. SNOW remains the only prior conversion in DB history.
- **2026-05-17** Off-universe held positions: AON, ASTS, SCHW, TEL appear in the universe inactive list but were held positions per the 2026-05-11 daily context. The retired "Universe rescore can orphan held positions" fix should still process them for Phase 3.5 trailing-stop and SELL handling. (from weekly_run_20260517)
  → 2026-05-18: Phase 3.5 evaluated all 4 (AON/ASTS/SCHW/TEL all show explicit "skipped (price ... below activation)" lines); Phase 3 also generated HOLD signals for each. SELL→`_close_long_position` routing is symbol-agnostic in `OrderManager.process` and doesn't need per-symbol verification; orphan-fix regression observably ruled out.
