"""
Backfill `trade_log.benchmark_return_pct` for rows where it is NULL.

For each NULL row, looks up the benchmark's daily close on entry_ts and exit_ts
(benchmark = config.data.benchmark_symbol, default SPY) and writes
    (bench_exit / bench_entry) - 1
into the row.  Rows whose entry_ts or exit_ts fall on a date with no stored
benchmark bar are left NULL (logged + counted).

Idempotent: only operates on `WHERE benchmark_return_pct IS NULL`, so re-running
is effectively a no-op for already-populated rows.

Run order (after Phase 1 lands):
    python scripts/run_pipeline.py
    python scripts/train_models.py --force
    python scripts/backfill_benchmark_returns.py

Wire into run_weekly.bat after `train_models.py --force` so each weekly retrain
auto-populates the new rows' benchmark returns.

Usage:
    python scripts/backfill_benchmark_returns.py
    python scripts/backfill_benchmark_returns.py --benchmark QQQ      # override
    python scripts/backfill_benchmark_returns.py --dry-run            # report only
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import config
from core.logger import get_logger
from data.database import OHLCVBar, TradeLog, get_engine

log = get_logger("scripts.backfill_benchmark_returns")


def _load_benchmark_bars(benchmark: str) -> pd.DataFrame:
    """Return all stored daily bars for `benchmark` as a date-indexed Series of closes.

    Index is `date` (no time component) so a lookup by trade_log.entry_ts.date()
    matches regardless of whether the bar timestamp is 04:00 UTC (US daily
    convention in this project) or any other time-of-day stamp.
    """
    engine = get_engine()
    with Session(engine) as session:
        rows = (
            session.query(OHLCVBar.timestamp, OHLCVBar.close)
            .filter(OHLCVBar.symbol == benchmark)
            .filter(OHLCVBar.interval == "1d")
            .order_by(OHLCVBar.timestamp)
            .all()
        )
    if not rows:
        return pd.DataFrame(columns=["date", "close"]).set_index("date")
    df = pd.DataFrame(rows, columns=["timestamp", "close"])
    df["date"] = pd.to_datetime(df["timestamp"]).dt.normalize()
    df = df.drop(columns=["timestamp"]).set_index("date").sort_index()
    return df


def _compute_return(closes: pd.DataFrame, entry_ts: datetime, exit_ts: datetime) -> float | None:
    """Return (close_exit / close_entry) - 1, or None when either bar is missing."""
    if closes.empty:
        return None
    entry_d = pd.Timestamp(entry_ts).normalize()
    exit_d  = pd.Timestamp(exit_ts).normalize()
    if entry_d not in closes.index or exit_d not in closes.index:
        return None
    c_entry = float(closes.loc[entry_d, "close"])
    c_exit  = float(closes.loc[exit_d,  "close"])
    if c_entry == 0:
        return None
    return (c_exit / c_entry) - 1.0


def backfill(benchmark: str | None = None, dry_run: bool = False) -> dict:
    """Walk trade_log rows with NULL benchmark_return_pct and populate them.

    Returns a summary dict: {benchmark, n_candidates, n_updated, n_missing_bar}.
    """
    benchmark = benchmark or config.data.benchmark_symbol
    log.info("Benchmark: %s | dry_run=%s", benchmark, dry_run)

    closes = _load_benchmark_bars(benchmark)
    if closes.empty:
        log.error(
            "No %s bars found in ohlcv_bars.  Run `python scripts/run_pipeline.py` "
            "first to seed benchmark data.", benchmark,
        )
        return {
            "benchmark":     benchmark,
            "n_candidates":  0,
            "n_updated":     0,
            "n_missing_bar": 0,
        }
    log.info("Loaded %d %s daily bars (%s -> %s)",
             len(closes), benchmark,
             closes.index.min().date(), closes.index.max().date())

    engine = get_engine()
    n_updated     = 0
    n_missing_bar = 0

    with Session(engine) as session:
        candidates = (
            session.query(TradeLog)
            .filter(TradeLog.benchmark_return_pct.is_(None))
            .all()
        )
        n_candidates = len(candidates)
        log.info("Found %d rows with NULL benchmark_return_pct", n_candidates)

        if n_candidates == 0:
            return {
                "benchmark":     benchmark,
                "n_candidates":  0,
                "n_updated":     0,
                "n_missing_bar": 0,
            }

        for row in candidates:
            ret = _compute_return(closes, row.entry_ts, row.exit_ts)
            if ret is None:
                n_missing_bar += 1
                continue
            if not dry_run:
                row.benchmark_return_pct = ret
            n_updated += 1

        if not dry_run:
            session.commit()

    log.info(
        "Backfill complete: %d updated, %d missing bar (left NULL), %d total candidates",
        n_updated, n_missing_bar, n_candidates,
    )
    return {
        "benchmark":     benchmark,
        "n_candidates":  n_candidates,
        "n_updated":     n_updated,
        "n_missing_bar": n_missing_bar,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill trade_log.benchmark_return_pct for NULL rows."
    )
    parser.add_argument("--benchmark", default=None,
                        help="Override config.data.benchmark_symbol (e.g. QQQ).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be updated without writing.")
    args = parser.parse_args()

    summary = backfill(benchmark=args.benchmark, dry_run=args.dry_run)

    print()
    print(f"=== Benchmark backfill summary ({summary['benchmark']}) ===")
    print(f"  candidates (NULL rows):    {summary['n_candidates']:,}")
    print(f"  updated:                   {summary['n_updated']:,}")
    print(f"  missing bar (left NULL):   {summary['n_missing_bar']:,}")
    if args.dry_run:
        print("  (dry-run — no rows written)")


if __name__ == "__main__":
    main()
