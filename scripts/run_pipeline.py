"""
Data pipeline — fetch OHLCV data, compute indicators, cache news, and score sentiment.

Run this once to seed the database, then use the dashboard for subsequent
refreshes (or schedule this script via cron / Task Scheduler for daily updates).

When config.universe.enabled is True, symbols are read from the universe_assets
table (populated by universe_scheduler.py).  Run the scheduler first:
    python universe_scheduler.py --run-now

Usage:
    python run_pipeline.py
    python run_pipeline.py --interval 1h
    python run_pipeline.py --skip-news          # skip news fetch + scoring (faster)
    python run_pipeline.py --skip-sentiment     # fetch news but skip FinBERT scoring
    python run_pipeline.py --use-watchlist      # force static watchlist even when
                                                #   config.universe.enabled is True
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from data.fetcher import DataFetcher
from data.indicators import IndicatorEngine


def main(interval: str = "1d", skip_news: bool = False,
         skip_sentiment: bool = False, use_watchlist: bool = False) -> None:

    # ── Symbol list: universe_assets table or static watchlist ──────────────────
    if config.universe.enabled and not use_watchlist:
        try:
            from data.database import get_universe_assets
            universe_df = get_universe_assets(active_only=True)
            if universe_df.empty:
                print(
                    "WARNING: universe_assets table is empty.  "
                    "Run `python universe_scheduler.py --run-now` first to populate it.\n"
                    "Falling back to static watchlist.\n"
                )
                watchlist = list(config.data.watchlist)
            else:
                watchlist = universe_df["symbol"].tolist()
                print(f"Universe: {len(watchlist)} active symbols from universe_assets")
        except Exception as exc:
            print(f"WARNING: Could not read universe_assets ({exc}) — falling back to watchlist.\n")
            watchlist = list(config.data.watchlist)
    else:
        watchlist = list(config.data.watchlist)
    print(f"Symbols : {watchlist}")
    print(f"Interval: {interval}")
    print(f"Database: {config.data.db_path}")
    print()

    fetcher = DataFetcher()
    engine  = IndicatorEngine()

    # ── VIX pre-cache (keeps RegimeDetector from blocking the Streamlit thread) ─
    print("=== VIX cache ===")
    print("  ^VIX ...", end=" ", flush=True)
    try:
        vix_df = fetcher.fetch_symbol("^VIX", interval="1d", days_back=30)
        if vix_df.empty:
            print("no data returned")
        else:
            print(f"{len(vix_df)} bars | latest close={vix_df['Close'].iloc[-1]:.2f}")
    except Exception as exc:
        print(f"failed ({exc})")

    # ── OHLCV + indicators ────────────────────────────────────────────────────
    print()
    print("=== Market data & indicators ===")
    for symbol in watchlist:
        print(f"  {symbol} ...", end=" ", flush=True)
        df = fetcher.fetch_symbol(symbol, interval=interval)
        if df.empty:
            print("no data returned")
            continue
        enriched = engine.run(symbol, interval=interval)
        last     = enriched.iloc[-1]
        rsi      = last.get("rsi_14")
        macd     = last.get("macd")
        rsi_str  = f"{rsi:.1f}" if rsi is not None and rsi == rsi else "n/a"
        macd_str = f"{macd:.3f}" if macd is not None and macd == macd else "n/a"
        print(f"{len(enriched)} bars | close={last['Close']:.2f} | "
              f"RSI={rsi_str} | MACD={macd_str}")

    if skip_news:
        print()
        print("Done.  Start the dashboard with:")
        print("  streamlit run dashboard/app.py")
        return

    # ── News fetch ────────────────────────────────────────────────────────────
    print()
    print("=== News cache (IBKR → Alpaca → yfinance) ===")
    from data.news_client import NewsClient
    from data.database import get_recent_news, upsert_news

    news_client = NewsClient()
    days_back   = config.alpaca.news_lookback_days
    all_unscored: dict[str, list[dict]] = {}   # symbol → articles needing scoring

    for symbol in watchlist:
        print(f"  {symbol} ...", end=" ", flush=True)
        try:
            articles = news_client.fetch_news(symbol, days_back=days_back,
                                              force_refresh=True)
            unscored = [a for a in articles if a.get("sentiment_score") is None]
            all_unscored[symbol] = unscored
            print(f"{len(articles)} articles cached  ({len(unscored)} unscored)")
        except Exception as exc:
            print(f"failed ({exc})")
            all_unscored[symbol] = []

    # ── FinBERT scoring ───────────────────────────────────────────────────────
    total_unscored = sum(len(v) for v in all_unscored.values())
    if skip_sentiment or total_unscored == 0:
        if total_unscored == 0:
            print("\nAll articles already scored — skipping FinBERT.")
        else:
            print("\nSkipping FinBERT scoring (--skip-sentiment).")
    else:
        print()
        print(f"=== FinBERT sentiment scoring ({total_unscored} articles) ===")
        print("  Loading model ...", end=" ", flush=True)
        from models.finbert_model import FinBERTModel
        finbert = FinBERTModel()
        pipe    = finbert._get_pipeline()
        if pipe is None:
            print("could not load FinBERT pipeline — skipping sentiment scoring.")
        else:
            print("ready.")
            for symbol, articles in all_unscored.items():
                if not articles:
                    continue
                scored = 0
                for art in articles:
                    try:
                        score = finbert._score_headline(art["headline"])
                        upsert_news(
                            symbol          = symbol,
                            article_id      = art["article_id"],
                            published_at    = art["published_at"],
                            headline        = art["headline"],
                            sentiment_score = score,
                        )
                        scored += 1
                    except Exception:
                        pass
                print(f"  {symbol}: scored {scored}/{len(articles)} articles")

    print()
    print("Done.  Start the dashboard with:")
    print("  streamlit run dashboard/app.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the trading database.")
    parser.add_argument("--interval",       default="1d",  help="Bar interval (default: 1d)")
    parser.add_argument("--skip-news",      action="store_true", help="Skip news fetch and scoring")
    parser.add_argument("--skip-sentiment", action="store_true", help="Fetch news but skip FinBERT scoring")
    parser.add_argument("--use-watchlist",  action="store_true",
                        help="Force static watchlist even when config.universe.enabled=True")
    args = parser.parse_args()
    main(interval=args.interval, skip_news=args.skip_news,
         skip_sentiment=args.skip_sentiment, use_watchlist=args.use_watchlist)
