"""
IBKR News API test script.

Connects to IB Gateway, lists your subscribed news providers, then fetches
recent headlines and one full article body for a sample symbol.

Run from the project root with IB Gateway open:
    python scripts/test_ibkr_news.py

Optional args:
    python scripts/test_ibkr_news.py --symbol MSFT --days 7 --max 20 --port 4002

The default --port value is taken from config.ibkr.paper_port (4002 for IB Gateway).
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="AAPL",  help="Ticker to fetch news for")
    p.add_argument("--days",   type=int, default=3, help="How many days back to search")
    p.add_argument("--max",    type=int, default=10, help="Max headlines to retrieve")
    p.add_argument("--port",   type=int, default=config.ibkr.paper_port,
                   help="IBKR API port (default: config.ibkr.paper_port — 4002=IB Gateway paper)")
    p.add_argument("--client-id", type=int, default=99, help="IBKR client ID (use one not in use)")
    return p.parse_args()


async def main():
    args = parse_args()

    try:
        from ib_insync import IB, Stock, util
    except ImportError:
        print("ERROR: ib_insync not installed.  Run: pip install ib_insync")
        sys.exit(1)

    util.logToConsole(level=30)   # WARNING only — suppress IB chatter

    ib = IB()
    print(f"\nConnecting to IBKR on {config.ibkr.host}:{args.port}, client_id={args.client_id} …")
    try:
        await ib.connectAsync(config.ibkr.host, args.port, clientId=args.client_id, timeout=10)
    except Exception as exc:
        print(f"ERROR: Could not connect to IBKR — {exc}")
        print("Make sure IB Gateway is open and the API is enabled.")
        print("  IB Gateway: Configure → Settings → API → Settings (paper port 4002)")
        sys.exit(1)

    print("Connected.\n")

    # ── 1. List available news providers ─────────────────────────────────────
    print("=" * 60)
    print("NEWS PROVIDERS AVAILABLE ON YOUR ACCOUNT")
    print("=" * 60)
    try:
        providers = await ib.reqNewsProvidersAsync()
        if not providers:
            print("  (none returned — check your market data subscriptions)")
        else:
            for p in providers:
                print(f"  {p.code:10s}  {p.name}")
        provider_codes = "+".join(p.code for p in providers) if providers else ""
    except Exception as exc:
        print(f"  reqNewsProviders failed: {exc}")
        provider_codes = ""

    if not provider_codes:
        print("\nNo news providers found.  Cannot continue.")
        ib.disconnect()
        return

    # ── 2. Resolve contract ID for the symbol ────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"RESOLVING CONTRACT FOR {args.symbol}")
    print("=" * 60)
    contract = Stock(args.symbol, "SMART", "USD")
    try:
        details = await ib.reqContractDetailsAsync(contract)
        if not details:
            print(f"  Could not resolve contract for {args.symbol}")
            ib.disconnect()
            return
        con_id = details[0].contract.conId
        print(f"  conId = {con_id}")
    except Exception as exc:
        print(f"  Contract resolution failed: {exc}")
        ib.disconnect()
        return

    # ── 3. Fetch historical headlines ─────────────────────────────────────────
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=args.days)

    # IBKR expects "YYYYMMDD HH:MM:SS" in UTC, or empty string for "now"
    start_str = start_dt.strftime("%Y%m%d %H:%M:%S")
    end_str   = end_dt.strftime("%Y%m%d %H:%M:%S")

    print(f"\n{'=' * 60}")
    print(f"HEADLINES FOR {args.symbol}  ({args.days} days back, max {args.max})")
    print(f"Providers: {provider_codes}")
    print(f"Window:    {start_str} → {end_str} UTC")
    print("=" * 60)

    headlines = []
    try:
        headlines = await ib.reqHistoricalNewsAsync(
            conId         = con_id,
            providerCodes = provider_codes,
            startDateTime = start_str,
            endDateTime   = end_str,
            totalResults  = args.max,
        )
    except Exception as exc:
        print(f"  reqHistoricalNews failed: {exc}")

    if not headlines:
        print("  No headlines returned.")
        print("\nPossible reasons:")
        print("  - No news for this symbol in the selected window")
        print("  - News subscription not fully enabled — check IBKR Client Portal → Market Data Subscriptions")
    else:
        print(f"  Retrieved {len(headlines)} headline(s):\n")
        for i, h in enumerate(headlines):
            ts = getattr(h, "time", "?")
            provider = getattr(h, "providerCode", "?")
            article_id = getattr(h, "articleId", "?")
            headline = getattr(h, "headline", "?")
            print(f"  [{i+1:2d}] {ts}  [{provider}]  {headline}")
            print(f"        articleId={article_id}")

    # ── 4. Fetch full body of the first article ───────────────────────────────
    if headlines:
        first = headlines[0]
        provider = getattr(first, "providerCode", None)
        article_id = getattr(first, "articleId", None)

        if provider and article_id:
            print(f"\n{'=' * 60}")
            print(f"FULL ARTICLE BODY — {provider} / {article_id}")
            print("=" * 60)
            try:
                article = await ib.reqNewsArticleAsync(
                    providerCode = provider,
                    articleId    = article_id,
                )
                art_type = getattr(article, "articleType", "?")
                art_text = getattr(article, "articleText", "")
                print(f"  Type: {art_type}")
                print(f"  Body ({len(art_text)} chars):\n")
                # Print first 1000 chars so it doesn't flood the terminal
                print(art_text[:1000])
                if len(art_text) > 1000:
                    print(f"\n  … ({len(art_text) - 1000} more characters truncated)")
            except Exception as exc:
                print(f"  reqNewsArticle failed: {exc}")

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print("=" * 60)
    print(f"  Providers available : {len(providers) if providers else 0}")
    print(f"  Headlines fetched   : {len(headlines)}")
    if headlines:
        times = [getattr(h, "time", None) for h in headlines if getattr(h, "time", None)]
        if times:
            print(f"  Oldest article      : {min(times)}")
            print(f"  Newest article      : {max(times)}")

    ib.disconnect()
    print("\nDisconnected from IBKR.")


if __name__ == "__main__":
    asyncio.run(main())
