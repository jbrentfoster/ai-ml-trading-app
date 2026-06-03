"""
Phase 2 — LLM news scoring (producer for the dashboard).

Reads cached article bodies (ingested by scripts/ingest_news_bodies.py), runs
the 8B extraction + composite scoring (models/llm_analyst.py), and writes one
row per (symbol, article_id, model) to ``llm_news_analysis``.

NO gateway needed — bodies come from SQLite.  This is the slow step (~80s /
article on the i5-1334U for 8B); run it in the clear overnight window or right
after the morning ingest.  Shadow workflow: nothing in the trading path reads
these rows.

Idempotent: only scores articles not already scored by the configured model.

    .venv/Scripts/python scripts/score_news_llm.py
    .venv/Scripts/python scripts/score_news_llm.py --symbols AAPL,NVDA --limit 5
    .venv/Scripts/python scripts/score_news_llm.py --model llama3.2:3b
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config           # noqa: E402
from core.logger import get_logger           # noqa: E402
from data.database import (                   # noqa: E402
    build_company_name_map,
    get_news_for_scoring,
    get_universe_assets,
    upsert_llm_analysis,
)
from models.llm_analyst import (  # noqa: E402
    LLMNewsAnalyst, resolve_attribution_status, status_is_mismatch, ATTR_DIGEST,
)

log = get_logger("scripts.score_news_llm")


def _resolve_symbols(args) -> list[str] | None:
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
    p.add_argument("--model", default=None, help="Ollama model tag (default config.llm.model)")
    p.add_argument("--min-chars", type=int, default=None, help="Skip bodies shorter than this")
    p.add_argument("--limit", type=int, default=None, help="Max articles to score this run")
    p.add_argument("--force", action="store_true",
                   help="Run even when config.llm.enabled is False (manual/testing)")
    return p.parse_args()


def main():
    args = parse_args()
    if not config.llm.enabled and not args.force:
        print("LLM news analyst disabled (config.llm.enabled=False). Use --force to run anyway.")
        return
    days = args.days if args.days is not None else config.llm.lookback_days
    model = args.model or config.llm.model
    min_chars = args.min_chars if args.min_chars is not None else config.llm.min_body_chars
    symbols = _resolve_symbols(args)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    name_map = build_company_name_map()

    rows = get_news_for_scoring(symbols, since, model, min_chars=min_chars)
    if args.limit:
        rows = rows[: args.limit]

    print(f"Model: {model} | symbols: {len(symbols)} | unscored articles (last {days}d): {len(rows)}")
    if not rows:
        print("Nothing to score.")
        return

    analyst = LLMNewsAnalyst(model=model)
    if not analyst.ping():
        print("Ollama not responding — is the server running and the model pulled?")
        log.warning("Ollama ping failed; aborting scoring run")
        return

    scored = parse_failures = 0
    reattributed = digests = 0
    for i, r in enumerate(rows):
        res = analyst.analyse(r["body"])
        # Headline-aware so multi-company digests aren't counted/flagged as
        # re-attributions (attributed_symbol is advisory — read path re-resolves).
        attributed, status = resolve_attribution_status(
            res.primary_entity, r["symbol"], name_map, headline=r["headline"])
        mismatch = status_is_mismatch(status)
        if mismatch:
            reattributed += 1
        if status == ATTR_DIGEST:
            digests += 1

        upsert_llm_analysis({
            "symbol":            r["symbol"],
            "article_id":        r["article_id"],
            "model":             model,
            "provider":          r["provider"],
            "published_at":      r["published_at"],
            "headline":          r["headline"],
            "event_type":        res.event_type,
            "direction":         res.direction,
            "magnitude":         res.magnitude,
            "time_horizon":      res.time_horizon,
            "novelty":           res.novelty,
            "confidence":        res.confidence,
            "entities":          json.dumps(res.entities, ensure_ascii=False),
            "primary_entity":    res.primary_entity,
            "attributed_symbol": attributed,
            "summary":           res.summary,
            "rationale":         res.rationale,
            "composite_score":   res.composite_score,
            "llm_direct_score":  res.llm_direct_score,
            "raw_response":      res.raw_response,
            "prompt_tokens":     res.prompt_tokens,
            "output_tokens":     res.output_tokens,
            "duration_ms":       res.duration_ms,
            "parse_ok":          res.parse_ok,
        })

        if res.parse_ok:
            scored += 1
        else:
            parse_failures += 1
        flag = f" ~{(res.primary_entity or '?')[:10]}" if mismatch else ""
        score_s = f"{res.composite_score:+.2f}" if res.composite_score is not None else "  n/a"
        print(f"  [{i+1:3d}/{len(rows)}] {r['symbol']:5s}{flag:8s} {score_s} "
              f"{res.direction or '?':7s} mag={res.magnitude} nov={res.novelty} "
              f"| {res.duration_ms/1000:4.1f}s | {(res.summary or '')[:60]}")

    print(f"\nScored {scored} | parse failures {parse_failures} | "
          f"re-attributed {reattributed} | digests {digests} (of {len(rows)})")
    log.info("LLM scoring: scored=%d parse_failures=%d reattributed=%d digests=%d model=%s",
             scored, parse_failures, reattributed, digests, model)


if __name__ == "__main__":
    main()
