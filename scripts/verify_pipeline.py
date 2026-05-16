"""
Step 2 Verification Script
===========================
End-to-end validation of the data pipeline:
  1. Fetch daily OHLCV bars from yfinance for the watchlist
  2. Verify bar counts and date ranges
  3. Compute and verify technical indicators
  4. Check data quality (no all-NaN columns, price sanity)
  5. Verify walk-forward splits work on the stored data

Run with:
    python verify_pipeline.py

No IBKR connection required.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows terminals default to cp1252; force UTF-8 so unicode output works.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from datetime import datetime, timezone

import pandas as pd

from config.settings import config
from data.database import get_bars, get_latest_indicators
from data.fetcher import DataFetcher
from data.indicators import IndicatorEngine, compute_indicators
from data.walk_forward import WalkForwardSplit

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "OK"
FAIL = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []   # (status, check, detail)


def check(status: str, label: str, detail: str = "") -> None:
    results.append((status, label, detail))
    icon = {"OK": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}.get(status, "[?]")
    line = f"{icon} {label}"
    if detail:
        line += f": {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    watchlist = config.data.watchlist
    interval  = "1d"

    print("=" * 60)
    print("  AI Trading App - Step 2 Pipeline Verification")
    print("=" * 60)
    print(f"  Watchlist : {watchlist}")
    print(f"  Interval  : {interval}")
    print(f"  Lookback  : {config.data.daily_lookback_days} days")
    print(f"  Database  : {config.data.db_path}")
    print("=" * 60)

    fetcher = DataFetcher()
    engine  = IndicatorEngine()

    # ── 1. Fetch data ─────────────────────────────────────────────────────────
    section("Check 1 — Fetching OHLCV data from yfinance")

    fetch_results: dict[str, pd.DataFrame] = {}
    for symbol in watchlist:
        print(f"  Fetching {symbol} ...", end=" ", flush=True)
        try:
            df = fetcher.fetch_symbol(symbol, interval=interval)
            fetch_results[symbol] = df
            print(f"{len(df)} bars")
        except Exception as exc:
            fetch_results[symbol] = pd.DataFrame()
            print(f"ERROR — {exc}")

    fetched_ok = [s for s, df in fetch_results.items() if not df.empty]
    fetched_fail = [s for s, df in fetch_results.items() if df.empty]

    check(PASS if not fetched_fail else WARN,
          f"Fetch complete: {len(fetched_ok)}/{len(watchlist)} symbols returned data",
          f"Failed: {fetched_fail}" if fetched_fail else "")

    if not fetched_ok:
        print("\n[FAIL] No data fetched - cannot continue.")
        sys.exit(1)

    # ── 2. Bar counts and date ranges ─────────────────────────────────────────
    section("Check 2 — Bar counts and date ranges")

    min_expected_bars = int(config.data.daily_lookback_days * 0.65)  # ~65% of calendar days are trading days

    for symbol in fetched_ok:
        df = fetch_results[symbol]
        n  = len(df)
        oldest = df.index[0].strftime("%Y-%m-%d")
        newest = df.index[-1].strftime("%Y-%m-%d")

        if n >= min_expected_bars:
            check(PASS, f"{symbol}: {n} bars  [{oldest} → {newest}]")
        else:
            check(WARN, f"{symbol}: only {n} bars (expected >= {min_expected_bars})",
                  f"[{oldest} → {newest}]")

    # ── 3. Compute indicators ─────────────────────────────────────────────────
    section("Check 3 — Computing technical indicators")

    indicator_results: dict[str, pd.DataFrame] = {}
    for symbol in fetched_ok:
        print(f"  Computing indicators for {symbol} ...", end=" ", flush=True)
        try:
            df = engine.run(symbol, interval=interval)
            indicator_results[symbol] = df
            print("done")
        except Exception as exc:
            indicator_results[symbol] = pd.DataFrame()
            print(f"ERROR — {exc}")

    computed_ok = [s for s, df in indicator_results.items() if not df.empty]
    check(PASS if len(computed_ok) == len(fetched_ok) else FAIL,
          f"Indicators computed for {len(computed_ok)}/{len(fetched_ok)} symbols")

    # ── 4. Data quality ───────────────────────────────────────────────────────
    section("Check 4 — Data quality")

    indicator_cols = [
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "bb_upper", "bb_middle", "bb_lower",
        "ema_9", "ema_21", "ema_50",
        "atr_14", "volume_sma_20",
    ]

    for symbol in computed_ok:
        df = indicator_results[symbol]

        # 4a. No all-NaN indicator columns
        all_nan_cols = [c for c in indicator_cols if c in df.columns and df[c].isna().all()]
        if all_nan_cols:
            check(FAIL, f"{symbol}: indicator columns are entirely NaN", str(all_nan_cols))
        else:
            # Count NaN-free tail rows (warm-up period expected at start)
            tail = df.tail(50)
            nan_pct = tail[indicator_cols].isna().mean().mean() * 100
            check(PASS if nan_pct < 5 else WARN,
                  f"{symbol}: indicator NaN rate in last 50 bars = {nan_pct:.1f}%")

        # 4b. Close prices are positive
        if (df["Close"] <= 0).any():
            check(FAIL, f"{symbol}: non-positive Close prices detected")
        else:
            check(PASS, f"{symbol}: Close prices all positive")

        # 4c. RSI in [0, 100]
        rsi = df["rsi_14"].dropna()
        if len(rsi) > 0 and not ((rsi >= 0) & (rsi <= 100)).all():
            check(FAIL, f"{symbol}: RSI values outside [0, 100]")
        else:
            check(PASS, f"{symbol}: RSI in valid range  (last={rsi.iloc[-1]:.1f})")

        # 4d. Chronological order
        if not df.index.is_monotonic_increasing:
            check(FAIL, f"{symbol}: index is not chronologically ordered")
        else:
            check(PASS, f"{symbol}: index is chronologically ordered")

        # 4e. No duplicate timestamps
        dupes = df.index.duplicated().sum()
        if dupes:
            check(FAIL, f"{symbol}: {dupes} duplicate timestamps")
        else:
            check(PASS, f"{symbol}: no duplicate timestamps")

    # ── 5. Database round-trip ────────────────────────────────────────────────
    section("Check 5 — Database round-trip")

    for symbol in computed_ok[:3]:   # spot-check first 3
        stored_df = get_bars(symbol, interval, limit=500)
        ind       = get_latest_indicators(symbol, interval)

        if stored_df.empty:
            check(FAIL, f"{symbol}: no bars found in database after upsert")
        else:
            check(PASS, f"{symbol}: {len(stored_df)} bars readable from database")

        if ind is None:
            check(FAIL, f"{symbol}: no indicator snapshot found in database")
        else:
            ts = ind["timestamp"].strftime("%Y-%m-%d")
            check(PASS, f"{symbol}: latest indicator snapshot dated {ts}  "
                  f"(RSI={ind['rsi_14']:.1f}, MACD={ind['macd']:.4f})")

    # ── 6. Walk-forward split sanity ──────────────────────────────────────────
    section("Check 6 — Walk-forward split on real data")

    ref_symbol = computed_ok[0]
    df_wf = indicator_results[ref_symbol]

    splitter = WalkForwardSplit(n_splits=5, train_bars=120, test_bars=30, gap_bars=1)

    try:
        folds = list(splitter.split(df_wf))

        check(PASS if len(folds) > 0 else FAIL,
              f"Splitter produced {len(folds)} fold(s) from {len(df_wf)} bars of {ref_symbol}")

        leakage = False
        for fold in folds:
            if fold.train_end >= fold.test_start:
                leakage = True
                check(FAIL, f"Fold {fold.fold_index}: train_end {fold.train_end.date()} "
                      f">= test_start {fold.test_start.date()} — DATA LEAKAGE")
        if not leakage:
            check(PASS, "No data leakage detected across all folds")

        if folds:
            print()
            print(splitter.summary(df_wf).to_string())

    except ValueError as exc:
        check(FAIL, f"Walk-forward splitter raised: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    passed = sum(1 for s, _, _ in results if s == PASS)
    warned = sum(1 for s, _, _ in results if s == WARN)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    total  = len(results)
    print(f"  Results: {passed} passed / {warned} warned / {failed} failed  ({total} checks)")
    print("=" * 60)

    if failed:
        print("\n[FAIL] Some checks failed - review output above.")
        sys.exit(1)
    elif warned:
        print("\n[WARN] All checks passed with warnings - review above before proceeding.")
    else:
        print("\n[PASS] All checks passed - ready for Step 3 (ML signal generation)")


if __name__ == "__main__":
    main()
