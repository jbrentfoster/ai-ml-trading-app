"""
One-time backfill of live trade history from an IBKR Flex Query XML export.

WHY THIS EXISTS (and why it is NOT part of normal operation):
``reqExecutions`` — the source Phase B reconciliation polls every daily run —
only retains ~7 days of execution history server-side (less in practice; see the
"IBKR reqExecutions retention is shorter than documented" architectural-decision
note in CLAUDE.md).  Any live fill that aged out before Phase B shipped
(2026-05-29) is therefore absent from ``fill_log`` / ``trade_log`` and is NOT
recoverable via the live path.

IBKR's *Flex Query* / Activity Statement path retains a year+ and includes the
same stable execution ID (``execID``) that ``reqExecutions`` returns.  So a Flex
export is the one source that can reach those aged-out fills.  This script parses
that XML, normalises each execution row into the **exact dict shape**
``IBKRConnection.get_executions`` produces (ibkr_connection.py:613-628), and feeds
it through the *same* ``execution.reconciliation.reconcile_fills`` core the daily
runner uses.  Nothing downstream is duplicated — fill_log ingest, round-trip
aggregation, the net-P&L convention, exit-reason inference, and both dedup guards
(fill_log on exec_id, trade_log on exit_exec_id) are all reused as-is.

IDEMPOTENT + SAFE TO OVERLAP with what Phase B already ingested: dedup is keyed
on ``execID``, so re-ingesting fills the live path already captured is a no-op.

After this runs once, the daily Phase B reconciliation keeps everything current —
this script is never needed again.

Usage:
    python scripts/backfill_flex_trades.py path/to/flex_trades.xml
    python scripts/backfill_flex_trades.py flex.xml --dry-run     # parse + show, no DB writes
    python scripts/backfill_flex_trades.py flex.xml --symbol SPY  # one symbol only

NO IB Gateway required — this reads a local file, not the live API.

FLEX QUERY SETUP (do once in the IBKR portal — see CLAUDE.md / the chat that
added this script): Trades section, **Level of Detail = Execution**, timezone
UTC if offered, fields incl. execID / conid / symbol / buySell / quantity /
tradePrice / ibCommission / orderType / orderID / accountId / dateTime.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import get_logger

log = get_logger("backfill_flex_trades")

# Flex dateTime can be emitted in several formats depending on the query's
# date/time-format setting.  Try the common ones in order.
_DATETIME_FORMATS = (
    "%Y%m%d;%H%M%S",      # 20260515;143000  (default semicolon form)
    "%Y%m%d %H%M%S",      # 20260515 143000
    "%Y-%m-%d;%H:%M:%S",  # 2026-05-15;14:30:00
    "%Y-%m-%d %H:%M:%S",  # 2026-05-15 14:30:00
    "%Y%m%d",             # 20260515 (date only — rare for executions)
)


def _parse_flex_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse a Flex ``dateTime`` string to a naive datetime.

    Treated as UTC by the reconciler (``_to_naive_utc`` passes naive datetimes
    through unchanged), so configure the Flex Query's timezone to UTC for honest
    entry/exit timestamps.  A wrong timezone does NOT cause duplicate rows —
    dedup is keyed on execID, not time — it only shifts displayed times.
    """
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    log.warning("Could not parse Flex dateTime %r — leaving exec_time None", raw)
    return None


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_utc_naive(dt: Optional[datetime], source_tz: Optional[str]) -> Optional[datetime]:
    """Convert a naive ``dt`` interpreted in ``source_tz`` to UTC-naive.

    Phase B stores ``exec_time`` in UTC; a Flex export defaults to the account's
    display timezone (commonly US/Eastern).  Pass ``--source-tz America/New_York``
    so backfilled rows match the live-path convention.  ``source_tz=None`` leaves
    the value untouched (use when the Flex Query itself was configured for UTC).
    DST is handled by ``zoneinfo`` (EST vs EDT resolved per-date).
    """
    if dt is None or not source_tz:
        return dt
    aware = dt.replace(tzinfo=ZoneInfo(source_tz))
    return aware.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_flex_trades(xml_text: str, source_tz: Optional[str] = None) -> list[dict]:
    """Parse Flex Query XML → execution dicts matching get_executions' shape.

    Filters to ``levelOfDetail="EXECUTION"`` rows (Flex can also emit ORDER and
    CLOSED_LOT level rows for the same fill — including them would double-count).
    Rows with no ``levelOfDetail`` attribute are kept (single-level exports).
    Non-stock rows (``assetCategory`` present and != "STK") are skipped — this
    system trades equities only.

    ``source_tz`` (e.g. "America/New_York") converts each ``exec_time`` from that
    timezone to UTC-naive so backfilled rows match Phase B's UTC convention.
    """
    root = ET.fromstring(xml_text)

    out: list[dict] = []
    skipped_level = 0
    skipped_nonstk = 0
    skipped_noexec = 0

    for tr in root.iter("Trade"):
        a = tr.attrib

        level = a.get("levelOfDetail")
        if level is not None and level.upper() != "EXECUTION":
            skipped_level += 1
            continue

        asset = a.get("assetCategory")
        if asset is not None and asset.upper() != "STK":
            skipped_nonstk += 1
            continue

        exec_id = a.get("execID") or a.get("ibExecID") or a.get("tradeID")
        if not exec_id:
            # No stable execution ID — can't dedup it; skip rather than risk
            # double-writing on a re-run.
            skipped_noexec += 1
            continue

        buysell = (a.get("buySell") or "").upper()
        side = "BUY" if buysell.startswith("B") else "SELL"

        qty = _to_float(a.get("quantity"))
        shares = abs(qty) if qty is not None else None

        order_id_raw = a.get("orderID") or a.get("ibOrderID")
        try:
            order_id = int(order_id_raw) if order_id_raw else None
        except ValueError:
            order_id = None

        conid_raw = a.get("conid")
        try:
            conid = int(conid_raw) if conid_raw else None
        except ValueError:
            conid = None

        # Flex ibCommission is a signed debit (e.g. -1.0); the reconciler wants a
        # positive cost (it subtracts commissions/position_value from pnl_pct).
        ib_comm = _to_float(a.get("ibCommission"))
        commission = abs(ib_comm) if ib_comm is not None else None

        price = _to_float(a.get("tradePrice"))

        out.append({
            "exec_id":         exec_id,
            "order_id":        order_id,
            "perm_id":         None,                      # not in Flex trades
            "parent_order_id": None,                      # not in Flex; reconciler falls back
            "account":         a.get("accountId"),
            "symbol":          a.get("symbol"),
            "conid":           conid,
            "side":            side,
            "order_type":      a.get("orderType") or None,
            "shares":          shares,
            "price":           round(price, 2) if price is not None else None,
            "commission":      commission,
            "realized_pnl":    _to_float(a.get("fifoPnlRealized")),
            "exec_time":       _to_utc_naive(
                                   _parse_flex_datetime(a.get("dateTime")
                                                        or a.get("tradeDate")),
                                   source_tz),
        })

    log.info(
        "Parsed %d execution row(s) from Flex XML "
        "(skipped: %d non-execution level, %d non-stock, %d missing execID)",
        len(out), skipped_level, skipped_nonstk, skipped_noexec,
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time backfill of live trades from an IBKR Flex Query XML export"
    )
    parser.add_argument("xml_path", help="Path to the downloaded Flex Query XML file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and show would-be writes; touch no tables")
    parser.add_argument("--symbol", default="", metavar="SYM",
                        help="Backfill a single symbol only")
    parser.add_argument("--source-tz", default="", metavar="TZ",
                        help="Timezone the Flex dateTime is in (e.g. "
                             "America/New_York). Converts exec_time to UTC to "
                             "match Phase B. Omit if the Flex Query is UTC.")
    args = parser.parse_args()

    xml_file = Path(args.xml_path)
    if not xml_file.is_file():
        print(f"ERROR: file not found: {xml_file}")
        return 2

    print("=== Flex trade backfill ===")
    if args.dry_run:
        print("  (dry-run — no DB writes)")

    try:
        xml_text = xml_file.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"ERROR: could not read {xml_file}: {exc}")
        return 2

    try:
        executions = parse_flex_trades(xml_text, source_tz=(args.source_tz or None))
    except ET.ParseError as exc:
        print(f"ERROR: malformed XML in {xml_file}: {exc}")
        return 2

    if not executions:
        print("  No execution rows found. Check the Flex Query has "
              "Level of Detail = Execution and the Trades section enabled.")
        return 1

    print(f"  Parsed {len(executions)} execution row(s) from {xml_file.name}")

    # Reuse the exact Phase B core.  The fetch callable just returns our parsed
    # list regardless of `since` — the reconciler ingests everything and bounds
    # client-side; dedup on exec_id / exit_exec_id makes overlap with already-
    # reconciled live fills a no-op.  `since=epoch` so window_start reporting
    # reflects "all history".
    from execution.reconciliation import reconcile_fills

    result = reconcile_fills(
        lambda _since: executions,
        since=datetime(2000, 1, 1),
        symbol=(args.symbol.upper() or None),
        dry_run=args.dry_run,
    )

    print(
        f"  New fills:      {result.n_new_fills}\n"
        f"  Cost-updated:   {result.n_cost_updated}\n"
        f"  Skipped fills:  {result.n_skipped_fills} (already ingested)\n"
        f"  Trades written: {result.n_trades_written}\n"
        f"  Trades skipped: {result.n_trades_skipped} (dedup — already reconciled)\n"
        f"  Deferred:       {result.n_deferred_cost} (commission missing in XML)\n"
        f"  Orphans:        {result.n_orphans} (entry with no matching exit in window)"
    )
    if result.n_orphans:
        print("  NOTE: orphans are positions still open at the end of the export "
              "(or whose entry/exit straddles the export's date range). Re-run "
              "with a wider Flex date range if a known round trip is missing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
