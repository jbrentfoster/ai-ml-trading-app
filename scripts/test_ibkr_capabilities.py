"""
IBKR capability sweep — probes which API endpoints/data feeds your paper-account
subscription has access to.  Read-only: nothing is ordered, nothing is modified.

Run from the project root with IB Gateway (or TWS) open:
    python scripts/test_ibkr_capabilities.py

Optional args:
    python scripts/test_ibkr_capabilities.py --symbol MSFT --port 4002

Each probe prints PASS / FAIL with the IBKR error code + message when it fails,
so subscription gaps are visible at a glance.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="AAPL", help="Ticker used for symbol-specific probes")
    p.add_argument("--port", type=int, default=config.ibkr.paper_port)
    p.add_argument("--client-id", type=int, default=98)
    return p.parse_args()


# ── Result tracking ─────────────────────────────────────────────────────────

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
results: list[tuple[str, str, str]] = []   # (label, status, detail)


def record(label: str, status: str, detail: str = "") -> None:
    results.append((label, status, detail))
    print(f"  {status} {label}{(' — ' + detail) if detail else ''}")


# ── Error capture ───────────────────────────────────────────────────────────

class ErrorTrap:
    """Captures error events so probes can correlate failures to IBKR codes."""

    def __init__(self, ib):
        self.errors: list[tuple[int, int, str]] = []   # (req_id, code, msg)
        ib.errorEvent += self._on_error

    def _on_error(self, req_id, code, msg, contract):
        self.errors.append((req_id, code, msg))

    def latest(self, since: int) -> list[tuple[int, int, str]]:
        return self.errors[since:]

    def __len__(self):
        return len(self.errors)


# ── Probes ──────────────────────────────────────────────────────────────────

async def probe_connection(ib):
    print("\n" + "=" * 60)
    print("CONNECTION & ACCOUNT")
    print("=" * 60)
    try:
        accounts = ib.managedAccounts()
        record("managedAccounts()", PASS, f"{accounts}")
    except Exception as exc:
        record("managedAccounts()", FAIL, str(exc))

    try:
        vals = await ib.accountSummaryAsync()
        record("accountSummaryAsync()", PASS, f"{len(vals)} tags returned")
    except Exception as exc:
        record("accountSummaryAsync()", FAIL, str(exc))


async def probe_market_data(ib, symbol: str, trap: ErrorTrap):
    print("\n" + "=" * 60)
    print("MARKET DATA — live vs delayed snapshot")
    print("=" * 60)

    from ib_insync import Stock

    contract = Stock(symbol, "SMART", "USD")

    # Resolve conId (used by several probes)
    con_id = None
    try:
        details = await ib.reqContractDetailsAsync(contract)
        if details:
            con_id = details[0].contract.conId
            record(f"reqContractDetailsAsync({symbol})", PASS, f"conId={con_id}")
        else:
            record(f"reqContractDetailsAsync({symbol})", FAIL, "no details returned")
    except Exception as exc:
        record(f"reqContractDetailsAsync({symbol})", FAIL, str(exc))

    # Live snapshot (market data type 1)
    try:
        before = len(trap)
        ib.reqMarketDataType(1)
        ticker = ib.reqMktData(contract, snapshot=True)
        await asyncio.sleep(2.5)
        ib.cancelMktData(contract)
        live_codes = [c for _, c, _ in trap.latest(before)]
        if 10167 in live_codes:
            record("Live market data (type 1)", FAIL, "code 10167 — no real-time subscription")
        else:
            mp = ticker.marketPrice()
            if mp and mp > 0 and not (mp != mp):  # NaN check
                record("Live market data (type 1)", PASS, f"last={mp:.2f}")
            else:
                record("Live market data (type 1)", FAIL, f"no price returned (codes={live_codes or 'none'})")
    except Exception as exc:
        record("Live market data (type 1)", FAIL, str(exc))

    # 15-min delayed snapshot
    try:
        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(contract, snapshot=False)
        await asyncio.sleep(3)
        mp = ticker.marketPrice()
        ib.cancelMktData(contract)
        ib.reqMarketDataType(1)
        if mp and mp > 0:
            record("Delayed market data (type 3)", PASS, f"last={mp:.2f}")
        else:
            record("Delayed market data (type 3)", FAIL, "no price returned")
    except Exception as exc:
        record("Delayed market data (type 3)", FAIL, str(exc))

    return con_id


async def probe_fundamentals(ib, symbol: str, trap: ErrorTrap):
    print("\n" + "=" * 60)
    print("REUTERS FUNDAMENTAL DATA  (reqFundamentalData)")
    print("Each report type is a separate subscription level.")
    print("=" * 60)

    from ib_insync import Stock

    contract = Stock(symbol, "SMART", "USD")

    # Standard IBKR fundamental report types.  Each may require a separate
    # subscription tier — Reuters Fundamentals (~$1.50/mo retail) covers
    # ReportSnapshot, ReportsFinSummary, ReportRatios, ReportsFinStatements;
    # RESC (analyst estimates) is a separate paid feed.
    report_types = [
        ("ReportSnapshot",        "Company snapshot — overview, ratios, recommendations"),
        ("ReportsFinSummary",     "Financial summary — multi-year"),
        ("ReportRatios",          "Detailed ratios"),
        ("ReportsFinStatements",  "Income / balance sheet / cash-flow statements"),
        ("RESC",                  "Reuters analyst estimates — earnings, revenue, target prices"),
        ("CalendarReport",        "Earnings dates & corporate events"),
    ]

    for rtype, desc in report_types:
        try:
            before = len(trap)
            data = await ib.reqFundamentalDataAsync(contract, rtype)
            errs = trap.latest(before)
            if data:
                record(f"{rtype:22s} ({desc})", PASS, f"{len(data)} chars XML")
            elif any(c in (430, 431, 10090, 10168) for _, c, _ in errs):
                code_msg = next((f"{c} — {m}" for _, c, m in errs if c in (430, 431, 10090, 10168)), "")
                record(f"{rtype:22s} ({desc})", FAIL, code_msg)
            else:
                record(f"{rtype:22s} ({desc})", FAIL, "empty response (no error)")
        except Exception as exc:
            record(f"{rtype:22s} ({desc})", FAIL, str(exc))


async def probe_news(ib):
    print("\n" + "=" * 60)
    print("NEWS PROVIDERS  (reqNewsProviders)")
    print("=" * 60)
    try:
        providers = await ib.reqNewsProvidersAsync()
        if providers:
            codes = ", ".join(p.code for p in providers)
            record("reqNewsProvidersAsync()", PASS, f"{len(providers)} providers: {codes}")
            for p in providers:
                print(f"      {p.code:12s} {p.name}")
        else:
            record("reqNewsProvidersAsync()", FAIL, "no providers returned")
    except Exception as exc:
        record("reqNewsProvidersAsync()", FAIL, str(exc))


async def probe_scanner(ib):
    print("\n" + "=" * 60)
    print("MARKET SCANNERS  (reqScannerParameters)")
    print("=" * 60)
    try:
        xml = await ib.reqScannerParametersAsync()
        if xml:
            # Quick stats — count scan codes available
            scan_count = xml.count("<ScanType>")
            instrument_count = xml.count("<Instrument>")
            record("reqScannerParametersAsync()", PASS,
                   f"{len(xml):,} chars XML | {scan_count} scan types | {instrument_count} instruments")
        else:
            record("reqScannerParametersAsync()", FAIL, "empty XML")
    except Exception as exc:
        record("reqScannerParametersAsync()", FAIL, str(exc))


async def probe_options(ib, symbol: str, con_id: int | None):
    print("\n" + "=" * 60)
    print("OPTIONS  (reqSecDefOptParams)")
    print("=" * 60)
    if con_id is None:
        record("reqSecDefOptParamsAsync()", SKIP, "no conId")
        return
    try:
        chains = await ib.reqSecDefOptParamsAsync(symbol, "", "STK", con_id)
        if chains:
            total_strikes = sum(len(c.strikes) for c in chains)
            total_expirations = sum(len(c.expirations) for c in chains)
            exchanges = sorted({c.exchange for c in chains})
            record("reqSecDefOptParamsAsync()", PASS,
                   f"{len(chains)} chain(s), exchanges={exchanges}, "
                   f"{total_strikes} strikes, {total_expirations} expirations")
        else:
            record("reqSecDefOptParamsAsync()", FAIL, "no chains returned")
    except Exception as exc:
        record("reqSecDefOptParamsAsync()", FAIL, str(exc))


async def probe_historical_head(ib, symbol: str):
    print("\n" + "=" * 60)
    print("HISTORICAL DATA RANGE  (reqHeadTimeStamp)")
    print("=" * 60)
    from ib_insync import Stock
    contract = Stock(symbol, "SMART", "USD")
    try:
        ts = await ib.reqHeadTimeStampAsync(contract, whatToShow="TRADES",
                                            useRTH=True, formatDate=1)
        if ts:
            record("reqHeadTimeStampAsync()", PASS, f"earliest bar: {ts}")
        else:
            record("reqHeadTimeStampAsync()", FAIL, "no timestamp returned")
    except Exception as exc:
        record("reqHeadTimeStampAsync()", FAIL, str(exc))


async def probe_pnl(ib):
    print("\n" + "=" * 60)
    print("REAL-TIME P&L SUBSCRIPTION  (reqPnL)")
    print("=" * 60)
    try:
        accounts = ib.managedAccounts()
        if not accounts:
            record("reqPnL()", SKIP, "no managed accounts")
            return
        account = accounts[0]
        pnl = ib.reqPnL(account, "")
        await asyncio.sleep(2)
        ib.cancelPnL(account, "")
        # ib_insync populates dailyPnL as nan until first event — just confirm the call didn't error
        record("reqPnL()", PASS,
               f"daily={pnl.dailyPnL}, unrealized={pnl.unrealizedPnL}, realized={pnl.realizedPnL}")
    except Exception as exc:
        record("reqPnL()", FAIL, str(exc))


async def probe_tick_by_tick(ib, symbol: str, trap: ErrorTrap):
    print("\n" + "=" * 60)
    print("TICK-BY-TICK DATA  (reqTickByTickData)")
    print("=" * 60)
    from ib_insync import Stock
    contract = Stock(symbol, "SMART", "USD")
    try:
        before = len(trap)
        ticker = ib.reqTickByTickData(contract, "Last", 0, False)
        await asyncio.sleep(2)
        ib.cancelTickByTickData(contract, "Last")
        errs = trap.latest(before)
        deny_codes = [c for _, c, _ in errs if c in (10090, 10168, 10169, 322)]
        if deny_codes:
            msg = next(m for _, c, m in errs if c in deny_codes)
            record("reqTickByTickData(Last)", FAIL, f"codes={deny_codes} — {msg}")
        else:
            record("reqTickByTickData(Last)", PASS,
                   f"{len(ticker.tickByTicks)} tick(s) in 2s window")
    except Exception as exc:
        record("reqTickByTickData(Last)", FAIL, str(exc))


async def probe_market_depth(ib, symbol: str, trap: ErrorTrap):
    print("\n" + "=" * 60)
    print("MARKET DEPTH / LEVEL 2  (reqMktDepth)")
    print("=" * 60)
    from ib_insync import Stock
    contract = Stock(symbol, "SMART", "USD")
    try:
        before = len(trap)
        ticker = ib.reqMktDepth(contract, numRows=5)
        await asyncio.sleep(2)
        ib.cancelMktDepth(contract)
        errs = trap.latest(before)
        deny_codes = [c for _, c, _ in errs if c in (10090, 10168, 309)]
        if deny_codes:
            msg = next(m for _, c, m in errs if c in deny_codes)
            record("reqMktDepth()", FAIL, f"codes={deny_codes} — {msg}")
        else:
            depth = len(ticker.domBids) + len(ticker.domAsks)
            record("reqMktDepth()", PASS, f"{depth} depth row(s) populated")
    except Exception as exc:
        record("reqMktDepth()", FAIL, str(exc))


async def probe_whatif(ib, symbol: str):
    print("\n" + "=" * 60)
    print("WHAT-IF ORDER  (pre-trade margin preview, no execution)")
    print("=" * 60)
    from ib_insync import Stock, MarketOrder
    contract = Stock(symbol, "SMART", "USD")
    order = MarketOrder("BUY", 1)
    order.whatIf = True
    try:
        state = await ib.whatIfOrderAsync(contract, order)
        if state:
            record("whatIfOrderAsync()", PASS,
                   f"commission={state.commission}, "
                   f"initMargin={state.initMarginChange}, "
                   f"maintMargin={state.maintMarginChange}")
        else:
            record("whatIfOrderAsync()", FAIL, "no state returned")
    except Exception as exc:
        record("whatIfOrderAsync()", FAIL, str(exc))


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    args = parse_args()

    try:
        from ib_insync import IB, util
    except ImportError:
        print("ERROR: ib_insync not installed.  Run: pip install ib_insync")
        sys.exit(1)

    util.logToConsole(level=40)   # ERROR only — suppress chatter

    ib = IB()
    print(f"Connecting to IBKR on {config.ibkr.host}:{args.port}, client_id={args.client_id} ...")
    try:
        await ib.connectAsync(config.ibkr.host, args.port,
                              clientId=args.client_id, timeout=10)
    except Exception as exc:
        print(f"ERROR: Could not connect to IBKR — {exc}")
        print("Is IB Gateway / TWS open and the API enabled?")
        sys.exit(1)
    print("Connected.\n")

    trap = ErrorTrap(ib)

    try:
        await probe_connection(ib)
        con_id = await probe_market_data(ib, args.symbol, trap)
        await probe_news(ib)
        await probe_fundamentals(ib, args.symbol, trap)
        await probe_scanner(ib)
        await probe_options(ib, args.symbol, con_id)
        await probe_historical_head(ib, args.symbol)
        await probe_pnl(ib)
        await probe_tick_by_tick(ib, args.symbol, trap)
        await probe_market_depth(ib, args.symbol, trap)
        await probe_whatif(ib, args.symbol)
    finally:
        ib.disconnect()

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    n_skip = sum(1 for _, s, _ in results if s == SKIP)
    print(f"  {n_pass} pass, {n_fail} fail, {n_skip} skip out of {len(results)} probes")
    print()
    for label, status, detail in results:
        line = f"  {status} {label}"
        if detail:
            line += f" — {detail[:80]}"
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
