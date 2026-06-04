"""
End-of-day bar refresh — overwrites mid-day partial bars with final post-close values.

Why this exists:
    signal_runner.py Phase 2 fetches OHLCV mid-day (~09:35-10:00 ET), so the
    daily bar yfinance returns is a *partial* snapshot.  Nothing else re-fetches
    that bar after the 16:00 ET close.  Symbols that subsequently drop out of
    the universe AND are no longer held never get their stale partial bar
    refreshed at all — the recorded "daily low/high/close" remains the mid-day
    snapshot forever.  This systematically hides intraday stop-outs, misleads
    Page 10's exit-reason analysis, and biases walk-forward retraining.

What this script does:
    Re-fetches the last ~5 trading days of bars for the union of:
      1. Current universe (or static watchlist if universe.enabled=False)
      2. Recently-acted symbols (order_decisions APPROVED/DRY_RUN in last 14 days)
      3. Recently-exited live positions (trade_log source='live' exit in last 14 days)
      4. Currently-held IBKR positions (optional — degrades cleanly if Gateway down)
    For each symbol it overwrites OHLCV rows in place (upsert_bars overwrite=True)
    and recomputes the derived indicator snapshots for those same dates.

    Why (3) is separate from (2): bracket fills (TP / stop / trailing) reconcile
    straight into trade_log as source='live' with NO order_decisions row, so the
    order_decisions-based union in (2) misses them.  A symbol that exits via a
    trailing stop drops out of both the universe and the held set immediately and
    stops getting bars fetched — observed on SNOW (exit 2026-05-21, last cached
    bar 2026-05-20).  Keeping recently-exited names in the union for a window
    lets post-exit bars keep flowing day by day (the rolling 5-day refresh on
    each EOD run overlaps to cover exit → exit+14d).

Scheduling:
    Designed to run at ~16:30 ET on weekdays via Windows Task Scheduler,
    AFTER market close so yfinance has the final daily bars.  See run_eod.bat.

Usage:
    python scripts/refresh_recent_bars.py              # default 5 days back
    python scripts/refresh_recent_bars.py --days 10    # wider backfill window
    python scripts/refresh_recent_bars.py --no-ibkr    # skip IBKR positions query
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from core.logger import get_logger
from data.database import (
    get_order_decisions,
    get_trade_log,
    get_universe_assets,
    upsert_indicators,
)
from data.fetcher import DataFetcher
from data.indicators import _INDICATOR_COLS, compute_indicators

log = get_logger("scripts.refresh_recent_bars")


def _universe_symbols() -> set[str]:
    """Active universe (or static watchlist) — same logic as run_pipeline.py."""
    if config.universe.enabled:
        try:
            df = get_universe_assets(active_only=True)
            if not df.empty:
                return set(df["symbol"].tolist())
        except Exception as exc:
            log.warning("Could not read universe_assets (%s) — falling back to watchlist", exc)
    return set(config.data.watchlist)


def _recently_acted_symbols(days: int = 14) -> set[str]:
    """Symbols with APPROVED or DRY_RUN order decisions in the last `days` days."""
    df = get_order_decisions(limit=2000)
    if df.empty:
        return set()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    recent = df[
        (df["decided_at"] >= cutoff)
        & (df["decision"].isin(["APPROVED", "DRY_RUN", "CLOSED_LONG"]))
    ]
    return set(recent["symbol"].tolist())


def _recently_exited_symbols(days: int = 14) -> set[str]:
    """Symbols whose *live* positions exited within the last `days` days.

    Sourced from trade_log (source='live'), NOT order_decisions: bracket exits
    (TP / stop / trailing) reconcile into trade_log with no order_decisions row,
    so `_recently_acted_symbols` can't see them.  This keeps a just-exited
    symbol in the refresh union long enough for its post-exit bars to be
    captured before it falls out of tracking entirely.
    """
    df = get_trade_log(source="live")
    if df.empty or "exit_ts" not in df.columns:
        return set()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    recent = df[df["exit_ts"] >= cutoff]
    return set(recent["symbol"].dropna().tolist())


def _ibkr_position_symbols() -> set[str]:
    """Currently-held IBKR positions.  Returns empty set if Gateway unreachable."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from execution.ibkr_connection import IBKRConnection
        ibkr = IBKRConnection()
        connected = loop.run_until_complete(ibkr.connect())
        if not connected:
            log.warning("IBKR Gateway unreachable — skipping held-positions union")
            return set()
        try:
            raw = loop.run_until_complete(ibkr.get_positions())
            return {p["symbol"] for p in raw if int(p.get("quantity", 0) or 0) != 0}
        finally:
            try:
                loop.run_until_complete(ibkr.disconnect())
            except Exception:
                pass
    except Exception as exc:
        log.warning("IBKR positions query failed (%s) — continuing without it", exc)
        return set()
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)


def _refresh_indicators(symbol: str, interval: str) -> int:
    """Recompute indicators from the (now-refreshed) bars and overwrite snapshots."""
    from data.database import get_bars
    bars = get_bars(symbol, interval, limit=500)
    if bars.empty:
        return 0
    enriched = compute_indicators(bars)
    ind_df = enriched[[c for c in _INDICATOR_COLS if c in enriched.columns]]
    return upsert_indicators(ind_df, symbol, interval, overwrite=True)


def main(days_back: int = 5, use_ibkr: bool = True, interval: str = "1d") -> None:
    print(f"=== End-of-day bar refresh (days_back={days_back}, interval={interval}) ===")
    started = datetime.now(timezone.utc).replace(tzinfo=None)

    universe = _universe_symbols()
    recent   = _recently_acted_symbols(days=14)
    exited   = _recently_exited_symbols(days=14)
    held     = _ibkr_position_symbols() if use_ibkr else set()
    # Benchmark (config.data.benchmark_symbol — SPY by default) is included
    # unconditionally so Page 10's relative-performance tracking always has
    # current bars, even if the user disables the universe or rotates the
    # benchmark out of permanent_fixtures.
    benchmark = {config.data.benchmark_symbol}

    symbols = sorted(universe | recent | exited | held | benchmark)
    print(f"Universe: {len(universe)} | Recent (14d): {len(recent)} | "
          f"Exited (14d): {len(exited)} | Held: {len(held)} | "
          f"Benchmark: {sorted(benchmark)} | Union: {len(symbols)}")
    print()

    if not symbols:
        print("No symbols to refresh.")
        return

    fetcher = DataFetcher()
    bars_total = 0
    ind_total  = 0
    failed: list[str] = []

    for sym in symbols:
        try:
            n_bars = fetcher.refresh_recent(sym, interval=interval, days_back=days_back)
            n_ind  = _refresh_indicators(sym, interval)
            bars_total += n_bars
            ind_total  += n_ind
            if n_bars > 0 or n_ind > 0:
                print(f"  {sym}: {n_bars} bar(s), {n_ind} indicator row(s) refreshed")
        except Exception as exc:
            failed.append(sym)
            log.error("Refresh failed for %s: %s", sym, exc, exc_info=True)
            print(f"  {sym}: FAILED — {exc}")

    duration = (datetime.now(timezone.utc).replace(tzinfo=None) - started).total_seconds()
    print()
    print(f"=== Done in {duration:.1f}s — {len(symbols)} symbols, "
          f"{bars_total} bars + {ind_total} indicator rows refreshed, "
          f"{len(failed)} failed ===")
    if failed:
        log.warning("Refresh failures: %s", failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-fetch recent OHLCV bars and overwrite stale rows.")
    parser.add_argument("--days", type=int, default=5,
                        help="Calendar days of history to re-fetch (default 5)")
    parser.add_argument("--no-ibkr", action="store_true",
                        help="Skip the IBKR-positions union (useful when Gateway is down)")
    parser.add_argument("--interval", default="1d", help="Bar interval (default 1d)")
    args = parser.parse_args()
    main(days_back=args.days, use_ibkr=not args.no_ibkr, interval=args.interval)
