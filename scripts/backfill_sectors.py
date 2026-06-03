"""
Backfill `fundamental_data.sector` for symbols whose latest snapshot has none.

Sector classification went data-driven (yfinance GICS sector captured at
fundamentals-fetch time, normalised at read time in
`risk/portfolio_guard.py:get_sector`) so the weekly-rotating dynamic universe
no longer calcifies the hardcoded `_SECTOR_MAP`.  Going forward the sector is
captured automatically by `FundamentalsClient._fetch_and_cache` on the next
fetch.  This one-time script fills the gap *now* — every pre-existing
`fundamental_data` row predates the new column, so without it the Account-page
sector view (and PortfolioGuard's sector cap) stay 'Unknown' until each symbol's
24h fundamentals cache expires and re-fetches.

By default targets the union of everything we've traded, decided on, or hold
(active universe ∪ watchlist ∪ order_decisions ∪ fill_log ∪ trade_log),
intersected with "latest fundamentals snapshot has a NULL sector".  This is
DB-driven (no Gateway needed) and bounded to a few hundred symbols, so it
catches held / rotated-out positions (e.g. C, GEV, VRT) without sweeping the
thousands of historical universe-scoring candidates that also live in
fundamental_data.  For each, fetches `yf.Ticker(sym).info['sector']` and writes
it onto that latest row (in place — no new snapshot, no other column touched).

Idempotent: only touches rows where the latest snapshot's sector IS NULL.
ETFs / fixtures are skipped automatically — yfinance returns no sector for them
and they're already covered by `_SECTOR_MAP`.

Usage:
    python scripts/backfill_sectors.py
    python scripts/backfill_sectors.py --dry-run          # report only
    python scripts/backfill_sectors.py --symbols C GEV    # explicit subset
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import config
from core.logger import get_logger
from data.database import FundamentalData, get_engine
from risk.portfolio_guard import _SECTOR_MAP, _normalize_yf_sector

log = get_logger("scripts.backfill_sectors")


def _target_symbols() -> list[str]:
    """Symbols the Account page / risk layer actually classify, needing a sector.

    Scope = the union of everything we've traded, decided on, or hold —
    (active universe ∪ watchlist ∪ order_decisions ∪ fill_log ∪ trade_log) —
    intersected with "latest fundamentals snapshot has a NULL sector".  This is
    DB-driven (no Gateway) and bounded to a few hundred symbols, so it covers
    held / rotated-out positions (C, GEV, VRT, …) without sweeping the thousands
    of historical universe-scoring candidates that also live in fundamental_data.
    """
    needs_sector = text("""
        SELECT f.symbol
        FROM fundamental_data f
        JOIN (
            SELECT symbol, MAX(fetched_at) AS latest
            FROM fundamental_data GROUP BY symbol
        ) m ON f.symbol = m.symbol AND f.fetched_at = m.latest
        WHERE f.sector IS NULL
    """)
    relevant = text("""
        SELECT symbol FROM universe_assets WHERE active = 1
        UNION SELECT symbol FROM order_decisions
        UNION SELECT symbol FROM fill_log
        UNION SELECT symbol FROM trade_log
    """)
    with get_engine().connect() as conn:
        null_set = {r[0] for r in conn.execute(needs_sector).fetchall() if r[0]}
        relevant_set = {r[0] for r in conn.execute(relevant).fetchall() if r[0]}
    relevant_set.update(config.data.watchlist)
    return sorted(relevant_set & null_set)


def _latest_row_needs_sector(session: Session, symbol: str) -> FundamentalData | None:
    """Return the symbol's newest fundamentals row iff its sector is NULL."""
    row = (
        session.query(FundamentalData)
        .filter(FundamentalData.symbol == symbol)
        .order_by(FundamentalData.fetched_at.desc())
        .first()
    )
    if row is not None and row.sector is None:
        return row
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="*", help="explicit symbol subset")
    parser.add_argument("--dry-run", action="store_true", help="report only; no writes")
    args = parser.parse_args()

    symbols = [s.upper() for s in (args.symbols or _target_symbols())]
    # ETFs/fixtures are map-covered and yfinance gives them no sector — skip.
    symbols = [s for s in symbols if s not in _SECTOR_MAP]

    engine = get_engine()
    filled = skipped_no_row = skipped_no_yf = 0

    with Session(engine) as session:
        for sym in symbols:
            row = _latest_row_needs_sector(session, sym)
            if row is None:
                # Either no fundamentals row yet, or sector already populated.
                skipped_no_row += 1
                continue
            try:
                raw = yf.Ticker(sym).info.get("sector") or None
            except Exception as exc:
                log.warning("yfinance sector fetch failed for %s: %s", sym, exc)
                raw = None
            if not raw:
                skipped_no_yf += 1
                continue
            bucket = _normalize_yf_sector(raw)
            log.info("%s -> %s (%s)", sym, bucket, raw)
            if not args.dry_run:
                row.sector = raw  # store raw GICS; normalised at read time
            filled += 1
        if not args.dry_run:
            session.commit()

    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"sector backfill: {filled} filled, "
        f"{skipped_no_row} skipped (no NULL-sector row), "
        f"{skipped_no_yf} skipped (yfinance had no sector)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
