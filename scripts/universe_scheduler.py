"""
Universe refresh scheduler.

Runs two recurring jobs:
  Sunday  02:00  — full refresh (all three stages)
  Mon-Sat 06:00  — Stage-3 re-score only (faster)

Usage:
    python universe_scheduler.py               # run forever (manual use only —
                                               #   production uses run_weekly.bat / run_daily.bat)
    python universe_scheduler.py --run-now     # one-shot full refresh then exit
    python universe_scheduler.py --rescore-now # one-shot Stage-3 re-score then exit
    python universe_scheduler.py --rescore-now --no-signal-run
                                               # skip the automatic signal_runner.py call
                                               # that normally follows a rescore.  Used by
                                               # run_daily.bat so signals run explicitly
                                               # AFTER train_models.py has caught up on
                                               # freshly promoted symbols.

Stage 3 ranks candidates by 20-day return + average dollar volume — no ML
model is loaded here.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schedule

from core.logger import get_logger
from config.settings import config
from data.universe import UniverseSelector

log = get_logger("universe_scheduler")


def _run_full() -> None:
    log.info("=== Universe: full refresh ===")
    try:
        result = UniverseSelector().run_full()
        log.info(
            "Full refresh done: S1=%d S2=%d S3=%d  (%.1fs)",
            result.stage1_count, result.stage2_count, result.stage3_count,
            result.duration_seconds,
        )
        log.info("Active candidates: %s", result.candidate_symbols)
    except Exception as exc:
        log.error("Universe full refresh failed: %s", exc, exc_info=True)


def _run_rescore(run_signals: bool = True) -> None:
    log.info("=== Universe: Stage-3 re-score ===")
    try:
        result = UniverseSelector().run_rescore()
        log.info(
            "Re-score done: %d candidates  (%.1fs)",
            result.stage3_count, result.duration_seconds,
        )
    except Exception as exc:
        log.error("Universe re-score failed: %s", exc, exc_info=True)

    if not run_signals:
        return

    # After re-scoring the universe, run the signal pipeline.
    # Pass dry_run=False so that OrderManager respects the config:
    # paper_orders_enabled=True → paper orders submitted; False → DRY_RUN.
    log.info("=== Signal runner (post-rescore) ===")
    try:
        from scripts.signal_runner import run as _signal_run
        _signal_run(dry_run=False)
    except Exception as exc:
        log.error("Post-rescore signal run failed: %s", exc, exc_info=True)


def _setup_schedule() -> None:
    # Full refresh every Sunday at 02:00
    schedule.every().sunday.at("02:00").do(_run_full)

    # Re-score every weekday at 06:00
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday"):
        getattr(schedule.every(), day).at("06:00").do(_run_rescore)

    log.info(
        "Scheduler configured: full refresh on Sundays at 02:00, "
        "re-score Mon-Sat at 06:00"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Universe refresh scheduler")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run a full refresh immediately and exit",
    )
    parser.add_argument(
        "--rescore-now",
        action="store_true",
        help="Run a Stage-3 re-score immediately and exit",
    )
    parser.add_argument(
        "--no-signal-run",
        action="store_true",
        help="Skip the post-rescore signal runner (use when batch file sequences "
             "training between rescore and signals)",
    )
    args = parser.parse_args()

    if args.run_now:
        _run_full()
        return

    if args.rescore_now:
        _run_rescore(run_signals=not args.no_signal_run)
        return

    _setup_schedule()
    print("Universe scheduler running. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
