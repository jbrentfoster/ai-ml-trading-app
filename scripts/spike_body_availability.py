"""
SPIKE (throwaway) — news article *body* availability across the universe.

Question this answers, with one number:
    Across a sample of our stage-3 universe, what fraction of news articles
    yield a USABLE FULL BODY (not a paywall stub, not a headline-only snippet)?

This is the prerequisite gate for the "local-LLM reads full article bodies"
idea.  FinBERT today scores HEADLINES only (news_cache stores no body), so
before building any extraction layer we need to know whether full bodies are
even reachable through our existing sources.

It reuses the proven IBKR fetch path from scripts/test_ibkr_news.py
(reqHistoricalNews -> reqNewsArticle) but turns the one-shot body proof into a
COVERAGE MEASUREMENT, broken down BY PROVIDER (critical: "IBKR" is not one
source — Dow Jones bodies are usually subscription stubs, Briefing.com bodies
are usually full).

Run from the project root with IB Gateway open:
    .venv/Scripts/python scripts/spike_body_availability.py
    .venv/Scripts/python scripts/spike_body_availability.py --symbols AAPL,MSFT --max 5
    .venv/Scripts/python scripts/spike_body_availability.py --from-universe --sample-size 12
    .venv/Scripts/python scripts/spike_body_availability.py --alpaca        # add Alpaca include_content pass

Deliverable = the printed per-provider table + 3-bucket verdict, plus a CSV
dump of every row (so the ~20 borderline 'stub' classifications can be
eyeballed by hand — the classifier WILL misjudge some).

Decision rule (set before running):
    >= 60% full   -> premise holds; proceed to shadow-mode extraction
    30-60% full   -> holds only for well-covered providers/symbols; scope to those
    <  30% full   -> headline-only is mostly what we have; needs a licensed feed or kill
"""

import argparse
import asyncio
import csv
import html
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config  # noqa: E402
from core.logger import get_logger  # noqa: E402

log = get_logger("scripts.spike_body_availability")

# ── classification thresholds ──────────────────────────────────────────────
FULL_MIN_LEN = 500     # >= this many cleaned chars (and no stub markers) -> full
STUB_MAX_LEN = 300     # < this many chars -> too short to be a real article body
ARTICLE_PAUSE = 0.15   # seconds between reqNewsArticle calls (gentle rate-limit)

# Paywall / "go read it elsewhere" markers — body present but not the real text.
STUB_MARKERS = (
    "subscribe",
    "subscription required",
    "available to subscribers",
    "full story",
    "to read this article",
    "to continue reading",
    "read the full",
    "for the full article",
    "click here to",
    "view the full",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Default sample — spans the coverage spectrum on purpose: DJ-heavy mega-caps
# (AAPL/MSFT/AMZN/...) plus mid-caps that tend to lean Briefing.com.  Symbols
# resolve for news regardless of whether they're in today's active universe.
DEFAULT_SAMPLE = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA",
    "JPM", "XOM", "UAL", "SNOW", "COHR", "MRVL", "WDC",
]


# ── helpers ─────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Best-effort tag strip + entity unescape so length/stub checks see prose."""
    if "<" in text and ">" in text:
        text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text.lower()).strip()


def classify(headline: str, raw_body: str, article_type) -> tuple[str, int, str]:
    """
    Return (klass, cleaned_len, cleaned_body) where klass is one of:
        full     usable article body
        stub     paywall marker, or body just restates the headline, or tiny
        snippet  short-but-not-obviously-paywalled (300-500 chars, no markers)
        fail     no body returned at all
    """
    if not raw_body or not raw_body.strip():
        return "fail", 0, ""

    text = _strip_html(raw_body)
    n = len(text)
    if n == 0:
        return "fail", 0, ""

    low = text.lower()
    if any(m in low for m in STUB_MARKERS):
        return "stub", n, text

    # Body that just restates the headline (+ a little) is a stub, not content.
    hn = _norm(headline)
    bn = _norm(text)
    if hn and (bn == hn or (hn in bn and n < len(headline) + 80)):
        return "stub", n, text

    if n < STUB_MAX_LEN:
        return "stub", n, text
    if n >= FULL_MIN_LEN:
        return "full", n, text
    return "snippet", n, text   # ambiguous 300-500 middle band


def _resolve_sample(args) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.from_universe:
        try:
            from data.database import get_universe_assets
            df = get_universe_assets(active_only=True)   # pandas DataFrame
            if df.empty or "symbol" not in df.columns:
                print("  (universe empty - falling back to default sample)")
                return DEFAULT_SAMPLE
            # Rank by stage3_score so the spread spans the ranking (the DataFrame
            # comes back ordered alphabetically by symbol otherwise).
            if "stage3_score" in df.columns:
                df = df.sort_values("stage3_score", ascending=False, na_position="last")
            syms = [s for s in df["symbol"].tolist() if s]
            if not syms:
                print("  (universe empty - falling back to default sample)")
                return DEFAULT_SAMPLE
            # Spread across the ranking so we don't sample only the top.
            step = max(1, len(syms) // args.sample_size)
            return syms[::step][: args.sample_size]
        except Exception as exc:
            print(f"  (could not read universe: {exc} - using default sample)")
            return DEFAULT_SAMPLE
    return DEFAULT_SAMPLE[: args.sample_size]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols", default=None,
                   help="Comma-separated tickers (overrides sampling)")
    p.add_argument("--from-universe", action="store_true",
                   help="Sample from active universe_assets instead of the default list")
    p.add_argument("--sample-size", type=int, default=12,
                   help="How many symbols to sample (default 12)")
    p.add_argument("--days", type=int, default=30,
                   help="How many days back to pull headlines (default 30)")
    p.add_argument("--max", type=int, default=25,
                   help="Max headlines per symbol to attempt body fetch on (default 25)")
    p.add_argument("--port", type=int, default=config.ibkr.paper_port,
                   help="IBKR API port (default config.ibkr.paper_port)")
    p.add_argument("--client-id", type=int, default=97,
                   help="IBKR client ID (use one not in use)")
    p.add_argument("--alpaca", action="store_true",
                   help="Also run an Alpaca include_content=True pass (needs API keys)")
    p.add_argument("--out", default=None,
                   help="CSV output path (default logs/spike_body_availability_<ts>.csv)")
    return p.parse_args()


# ── IBKR pass ────────────────────────────────────────────────────────────────

async def _ibkr_pass(ib, Stock, symbols, args, rows):
    """Fetch headlines + bodies for each symbol; append classified rows."""
    providers = await ib.reqNewsProvidersAsync()
    provider_codes = "+".join(p.code for p in providers) if providers else ""
    print(f"\nSubscribed news providers ({len(providers) if providers else 0}): "
          f"{', '.join(p.code for p in providers) if providers else '(none)'}")
    if not provider_codes:
        print("No news providers on this account — IBKR pass cannot run.")
        return

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=args.days)
    start_str = start_dt.strftime("%Y%m%d %H:%M:%S")
    end_str = end_dt.strftime("%Y%m%d %H:%M:%S")

    for sym in symbols:
        try:
            details = await ib.reqContractDetailsAsync(Stock(sym, "SMART", "USD"))
            if not details:
                print(f"  [{sym}] contract unresolved — skipping")
                continue
            con_id = details[0].contract.conId
            headlines = await ib.reqHistoricalNewsAsync(
                conId=con_id,
                providerCodes=provider_codes,
                startDateTime=start_str,
                endDateTime=end_str,
                totalResults=args.max,
            )
        except Exception as exc:
            print(f"  [{sym}] headline fetch failed: {exc}")
            continue

        n_full = 0
        for h in headlines:
            provider = getattr(h, "providerCode", "?")
            article_id = getattr(h, "articleId", "")
            headline = getattr(h, "headline", "") or ""
            pub = getattr(h, "time", "")
            art_type, body = "?", ""
            try:
                art = await ib.reqNewsArticleAsync(providerCode=provider, articleId=article_id)
                art_type = getattr(art, "articleType", "?")
                body = getattr(art, "articleText", "") or ""
            except Exception as exc:
                log.debug("reqNewsArticle failed for %s/%s: %s", provider, article_id, exc)

            klass, blen, cleaned = classify(headline, body, art_type)
            if klass == "full":
                n_full += 1
            rows.append({
                "symbol": sym,
                "source": "IBKR",
                "provider": provider,
                "article_id": article_id,
                "published_at": str(pub),
                "article_type": art_type,
                "body_len": blen,
                "class": klass,
                "headline": headline[:120],
                "excerpt": cleaned[:200],
            })
            time.sleep(ARTICLE_PAUSE)

        print(f"  [{sym}] {len(headlines):3d} headlines -> {n_full:3d} full bodies")


# ── Alpaca pass (optional) ───────────────────────────────────────────────────

def _alpaca_pass(symbols, args, rows):
    api_key = config.alpaca.api_key
    secret_key = config.alpaca.secret_key
    if not api_key or not secret_key:
        print("\nAlpaca pass skipped — ALPACA_API_KEY / ALPACA_SECRET_KEY not set.")
        return
    try:
        from alpaca.data.historical import NewsClient as AlpacaNewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError:
        print("\nAlpaca pass skipped — alpaca-py not installed.")
        return

    client = AlpacaNewsClient(api_key=api_key, secret_key=secret_key)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    print("\nAlpaca pass (include_content=True):")
    for sym in symbols:
        try:
            req = NewsRequest(symbols=sym, start=since, limit=args.max,
                              sort="desc", include_content=True)
            resp = client.get_news(req)
            if hasattr(resp, "data") and isinstance(resp.data, dict):
                articles = resp.data.get("news", [])
            elif hasattr(resp, "news"):
                articles = resp.news
            else:
                articles = list(resp)
        except Exception as exc:
            print(f"  [{sym}] Alpaca fetch failed: {exc}")
            continue

        n_full = 0
        for art in articles:
            headline = getattr(art, "headline", "") or ""
            body = getattr(art, "content", "") or ""
            klass, blen, cleaned = classify(headline, body, "html")
            if klass == "full":
                n_full += 1
            rows.append({
                "symbol": sym,
                "source": "Alpaca",
                "provider": getattr(art, "source", "alpaca") or "alpaca",
                "article_id": str(getattr(art, "id", "")),
                "published_at": str(getattr(art, "created_at", "")),
                "article_type": "html",
                "body_len": blen,
                "class": klass,
                "headline": headline[:120],
                "excerpt": cleaned[:200],
            })
        print(f"  [{sym}] {len(articles):3d} articles -> {n_full:3d} full bodies")


# ── reporting ────────────────────────────────────────────────────────────────

def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _report(rows):
    if not rows:
        print("\nNo rows collected — nothing to report.")
        return

    print("\n" + "=" * 78)
    print("BY SOURCE x PROVIDER")
    print("=" * 78)
    print(f"{'source/provider':22s} {'n':>4s} {'full%':>6s} {'stub%':>6s} "
          f"{'snip%':>6s} {'fail%':>6s} {'med_full_len':>12s}")
    by_prov = defaultdict(list)
    for r in rows:
        by_prov[(r["source"], r["provider"])].append(r)
    for (source, prov), items in sorted(by_prov.items(),
                                        key=lambda kv: -len(kv[1])):
        c = Counter(i["class"] for i in items)
        n = len(items)
        full_lens = [i["body_len"] for i in items if i["class"] == "full"]
        med = int(median(full_lens)) if full_lens else 0
        print(f"{source + '/' + prov:22.22s} {n:4d} "
              f"{_pct(c['full'], n):6.1f} {_pct(c['stub'], n):6.1f} "
              f"{_pct(c['snippet'], n):6.1f} {_pct(c['fail'], n):6.1f} {med:12d}")

    print("\n" + "=" * 78)
    print("BY SYMBOL")
    print("=" * 78)
    print(f"{'symbol':10s} {'n':>4s} {'full%':>6s}")
    by_sym = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    for sym, items in sorted(by_sym.items(),
                             key=lambda kv: -_pct(Counter(i["class"] for i in kv[1])["full"], len(kv[1]))):
        c = Counter(i["class"] for i in items)
        print(f"{sym:10s} {len(items):4d} {_pct(c['full'], len(items)):6.1f}")

    # ── verdict ──
    total = len(rows)
    full = sum(1 for r in rows if r["class"] == "full")
    rate = _pct(full, total)
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"  Articles attempted : {total}")
    print(f"  Full bodies        : {full}  ({rate:.1f}%)")
    if rate >= 60:
        bucket = ">= 60%  -> PREMISE HOLDS: proceed to shadow-mode extraction"
    elif rate >= 30:
        bucket = "30-60%  -> HOLDS for well-covered providers only: scope to those"
    else:
        bucket = "<  30%  -> headline-only is mostly what we have: needs licensed feed or kill"
    print(f"  Bucket             : {bucket}")
    print("\n  NOTE: scan the CSV's 'stub' rows by hand - the classifier mislabels some.")


def _write_csv(rows, out_path):
    fields = ["symbol", "source", "provider", "article_id", "published_at",
              "article_type", "body_len", "class", "headline", "excerpt"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"\nRaw rows written to: {out_path}")


# ── main ─────────────────────────────────────────────────────────────────────

async def main():
    args = parse_args()
    symbols = _resolve_sample(args)
    print(f"Sample ({len(symbols)} symbols): {', '.join(symbols)}")
    print(f"Window: last {args.days} days, max {args.max} headlines/symbol")

    rows: list[dict] = []

    try:
        from ib_insync import IB, Stock, util
    except ImportError:
        print("ERROR: ib_insync not installed. Run: pip install ib_insync")
        sys.exit(1)

    util.logToConsole(level=40)   # ERROR only — suppress IB chatter

    ib = IB()
    print(f"\nConnecting to IBKR on {config.ibkr.host}:{args.port}, "
          f"client_id={args.client_id} ...")
    try:
        await ib.connectAsync(config.ibkr.host, args.port,
                              clientId=args.client_id, timeout=10)
        print("Connected.")
        await _ibkr_pass(ib, Stock, symbols, args, rows)
    except Exception as exc:
        print(f"ERROR: IBKR connection/fetch failed — {exc}")
        print("Make sure IB Gateway is open and the API is enabled (paper port 4002).")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    if args.alpaca:
        _alpaca_pass(symbols, args, rows)

    _report(rows)

    if rows:
        out_path = args.out
        if not out_path:
            logs_dir = Path(__file__).resolve().parent.parent / "logs"
            logs_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = str(logs_dir / f"spike_body_availability_{ts}.csv")
        _write_csv(rows, out_path)


if __name__ == "__main__":
    asyncio.run(main())
