"""
Phase 1 — news body ingestion (producer for the LLM news analyst).

Back-fills ``news_cache.body`` with full article text for stage-3 universe
articles whose headline is already cached (by run_pipeline.py) but whose body
is still NULL.  Fetches via IBKR ``reqNewsArticle`` — the only source the spike
found reliably returns full bodies (~95%).

This step needs IB Gateway up; it piggybacks the morning window where the
gateway is already open for signal_runner.  It is deliberately SEPARATE from
the LLM scoring pass (scripts/score_news_llm.py), which reads bodies from
SQLite and needs no gateway — so the slow, gateway-free work can run anytime.

Idempotent: only touches rows where body IS NULL, so re-runs are cheap no-ops.

    .venv/Scripts/python scripts/ingest_news_bodies.py
    .venv/Scripts/python scripts/ingest_news_bodies.py --symbols AAPL,NVDA --days 3
    .venv/Scripts/python scripts/ingest_news_bodies.py --no-universe   # use static watchlist
"""

import argparse
import asyncio
import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config           # noqa: E402
from core.logger import get_logger           # noqa: E402
from data.database import (                   # noqa: E402
    get_news_needing_body,
    get_universe_assets,
    set_news_body,
)

log = get_logger("scripts.ingest_news_bodies")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _resolve_symbols(args) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not args.no_universe and config.universe.enabled:
        df = get_universe_assets(active_only=True)
        if not df.empty and "symbol" in df.columns:
            return [s for s in df["symbol"].tolist() if s]
    return list(config.data.watchlist)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=None, help="Comma-separated tickers (overrides universe)")
    p.add_argument("--no-universe", action="store_true", help="Use static watchlist instead of universe")
    p.add_argument("--days", type=int, default=None, help="Lookback days (default config.llm.lookback_days)")
    p.add_argument("--min-chars", type=int, default=None, help="Skip bodies shorter than this (default config.llm.min_body_chars)")
    p.add_argument("--limit", type=int, default=None, help="Max articles to fetch this run (safety cap)")
    p.add_argument("--port", type=int, default=config.ibkr.paper_port)
    p.add_argument("--client-id", type=int, default=config.ibkr.client_id + 60)
    p.add_argument("--force", action="store_true",
                   help="Run even when config.llm.enabled is False (manual/testing)")
    return p.parse_args()


async def _run(args, symbols, since, min_chars):
    rows = get_news_needing_body(symbols, since)
    # Only IBKR-tagged articles (provider$id) have a fetchable body.
    rows = [r for r in rows if "$" in r["article_id"]]
    if args.limit:
        rows = rows[: args.limit]

    print(f"Symbols: {len(symbols)} | articles needing body (last {args.days}d): {len(rows)}")
    if not rows:
        print("Nothing to ingest.")
        return

    from ib_insync import IB, util
    util.logToConsole(level=40)
    ib = IB()
    print(f"Connecting to IBKR on {config.ibkr.host}:{args.port} ...")
    try:
        await ib.connectAsync(config.ibkr.host, args.port, clientId=args.client_id, timeout=10)
    except Exception as exc:
        log.warning("IBKR unreachable (%s) — cannot ingest bodies this run", exc)
        print(f"IBKR unreachable: {exc}")
        return
    print("Connected.")

    stored = too_short = failed = 0
    try:
        for r in rows:
            provider, _, raw_id = r["article_id"].partition("$")
            if not provider or not raw_id:
                continue
            try:
                art = await ib.reqNewsArticleAsync(providerCode=provider, articleId=raw_id)
                body = _strip_html(getattr(art, "articleText", "") or "")
            except Exception as exc:
                log.debug("reqNewsArticle failed for %s: %s", r["article_id"], exc)
                failed += 1
                continue
            if len(body) < min_chars:
                too_short += 1
                continue
            if set_news_body(r["symbol"], r["article_id"], body):
                stored += 1
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    print(f"\nStored {stored} bodies | {too_short} too short (<{min_chars}) | {failed} fetch failures")
    log.info("Body ingestion: stored=%d too_short=%d failed=%d (of %d candidates)",
             stored, too_short, failed, len(rows))


def main():
    args = parse_args()
    if not config.llm.enabled and not args.force:
        print("LLM news analyst disabled (config.llm.enabled=False). Use --force to run anyway.")
        return
    args.days = args.days if args.days is not None else config.llm.lookback_days
    min_chars = args.min_chars if args.min_chars is not None else config.llm.min_body_chars
    symbols = _resolve_symbols(args)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
    asyncio.run(_run(args, symbols, since, min_chars))


if __name__ == "__main__":
    main()
