"""
NewsClient — unified news fetcher with three-tier fallback.

Priority order:
  1. IBKR        (reqHistoricalNews — Dow Jones, Briefing.com; ~4-5 months history)
  2. Alpaca       (News API — requires ALPACA_API_KEY / ALPACA_SECRET_KEY)
  3. yfinance     (no API key; ~10 most-recent articles only)

All sources write to the same news_cache SQLite table via upsert_news so
FinBERT does not score the same article twice regardless of which source
fetched it.

Deduplication key: (symbol, article_id)
  - IBKR:    providerCode$articleId  e.g. "DJ-N$1e1c149c"
  - Alpaca:  numeric string from their API
  - yfinance: URL or uuid derived from the item

The IBKR client reuses an existing ib_insync IB instance when one is
passed in, or opens (and immediately closes) a short-lived connection
when called standalone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from config.settings import config
from core.logger import get_logger
from data.database import get_recent_news, upsert_news

if TYPE_CHECKING:
    pass

log = get_logger("data.news_client")

# Dow Jones wraps some headlines with metadata: {A:800015:L:en}headline text
_DJ_PREFIX = re.compile(r"^\{[^}]+\}")


def _strip_dj_prefix(headline: str) -> str:
    return _DJ_PREFIX.sub("", headline).strip()


class NewsClient:
    """
    Unified news client: IBKR → Alpaca → yfinance.

    Parameters
    ----------
    ib_instance :
        An already-connected ib_insync.IB object.  When provided, IBKR news
        is attempted first.  Pass None (default) to skip IBKR and fall
        straight to Alpaca/yfinance.
    """

    def __init__(self, ib_instance=None) -> None:
        self._ib      = ib_instance   # optional live IB connection
        self._alpaca  = None          # lazy

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_news(
        self,
        symbol: str,
        days_back: int | None = None,
        force_refresh: bool = False,
    ) -> list[dict]:
        """
        Return recent news articles for `symbol`.

        Checks the SQLite cache first unless `force_refresh=True`, in which
        case it calls the upstream API and merges results into the cache.

        Returns a list of dicts:
            article_id, published_at, headline, sentiment_score (None until scored)
        """
        if days_back is None:
            days_back = config.alpaca.news_lookback_days

        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_back)

        if not force_refresh:
            cached = get_recent_news(symbol, since)
            if cached:
                log.debug("Returning %d cached news items for %s", len(cached), symbol)
                return cached

        # Cascade through sources in priority order.  Fall through to the next
        # tier whenever the current tier returned nothing OR its newest article
        # is older than STALE_THRESHOLD_HOURS — otherwise a thinly-covered
        # symbol on IBKR's Dow Jones feed (e.g. AKAM) would permanently mask
        # newer yfinance articles.  Deduplication is handled inside
        # upsert_news via (symbol, article_id).
        fetched = self._fetch_from_ibkr(symbol, since, days_back)
        if self._needs_fallback(fetched):
            alpaca_articles = self._fetch_from_alpaca(symbol, since)
            fetched = fetched + alpaca_articles
        if self._needs_fallback(fetched):
            yf_articles = self._fetch_from_yfinance(symbol, since)
            fetched = fetched + yf_articles

        return get_recent_news(symbol, since)

    STALE_THRESHOLD_HOURS = 24

    @classmethod
    def _needs_fallback(cls, articles: list[dict]) -> bool:
        """True if `articles` is empty or every article is > STALE_THRESHOLD_HOURS old."""
        if not articles:
            return True
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        latest = max(
            (a["published_at"] for a in articles if a.get("published_at")),
            default=None,
        )
        if latest is None:
            return True
        age_hours = (now - latest).total_seconds() / 3600.0
        return age_hours > cls.STALE_THRESHOLD_HOURS

    # ── IBKR source ───────────────────────────────────────────────────────────

    def _fetch_from_ibkr(
        self, symbol: str, since: datetime, days_back: int
    ) -> list[dict]:
        """
        Fetch news via IBKR reqHistoricalNews.

        Uses the IB instance passed at construction time.  Returns [] if IBKR
        is not available or returns no articles.
        """
        if self._ib is None:
            try:
                return self._fetch_from_ibkr_standalone(symbol, since, days_back)
            except Exception as exc:
                log.debug("IBKR standalone news fetch failed (%s); falling back", exc)
                return []

        try:
            return self._do_ibkr_fetch(self._ib, symbol, since, days_back)
        except Exception as exc:
            log.debug("IBKR news fetch failed (%s); falling back", exc)
            return []

    def _fetch_from_ibkr_standalone(
        self, symbol: str, since: datetime, days_back: int
    ) -> list[dict]:
        """Open a short-lived IBKR connection, fetch, then close.

        Creates its own asyncio event loop *before* importing ib_insync so
        this method is safe to call from non-main threads (e.g. Streamlit's
        ScriptRunner thread).  eventkit (used internally by ib_insync) calls
        asyncio.get_event_loop() at import time, so the loop must exist first.
        """
        import asyncio

        # Must set the event loop before importing ib_insync / eventkit,
        # which calls asyncio.get_event_loop() when the module is first loaded.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            from ib_insync import IB, util  # type: ignore
        except ImportError:
            loop.close()
            log.debug("ib_insync not installed; skipping IBKR news source")
            return []
        except Exception as exc:
            loop.close()
            log.debug("ib_insync import failed (%s); skipping IBKR news source", exc)
            return []

        cfg  = config.ibkr
        port = cfg.paper_port  # always use paper port for news (read-only)

        ib = IB()
        try:
            util.logToConsole(level=40)   # ERROR only during short-lived connections
            ib.connect("127.0.0.1", port, clientId=cfg.client_id + 50, timeout=5)
        except Exception as exc:
            log.debug("IBKR not reachable for news fetch (%s); skipping", exc)
            loop.close()
            return []

        try:
            return self._do_ibkr_fetch(ib, symbol, since, days_back)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
            loop.close()

    def _do_ibkr_fetch(self, ib, symbol: str, since: datetime, days_back: int) -> list[dict]:
        """Core IBKR fetch logic given a connected IB instance."""
        try:
            from ib_insync import Stock  # type: ignore
        except ImportError:
            return []

        try:
            # Resolve contract
            contract = Stock(symbol, "SMART", "USD")
            details  = ib.reqContractDetails(contract)
            if not details:
                log.warning("IBKR: could not resolve contract for %s", symbol)
                return []
            con_id = details[0].contract.conId

            # Get subscribed providers
            providers = ib.reqNewsProviders()
            if not providers:
                log.warning("IBKR: no news providers available")
                return []
            provider_codes = "+".join(p.code for p in providers)

            # Date range — IBKR wants "YYYYMMDD HH:MM:SS" UTC strings
            end_dt   = datetime.now(timezone.utc)
            start_dt = end_dt - timedelta(days=days_back)
            start_str = start_dt.strftime("%Y%m%d %H:%M:%S")
            end_str   = end_dt.strftime("%Y%m%d %H:%M:%S")

            headlines = ib.reqHistoricalNews(
                conId         = con_id,
                providerCodes = provider_codes,
                startDateTime = start_str,
                endDateTime   = end_str,
                totalResults  = config.alpaca.max_articles_per_symbol,
            )
        except Exception as exc:
            log.warning("IBKR news fetch failed for %s: %s", symbol, exc)
            return []

        results: list[dict] = []
        for h in headlines:
            try:
                raw_headline = getattr(h, "headline", "") or ""
                headline     = _strip_dj_prefix(raw_headline)
                if not headline:
                    continue

                provider   = getattr(h, "providerCode", "IBKR")
                article_id = f"{provider}${getattr(h, 'articleId', '')}"

                # IBKR returns datetime objects or ISO strings
                pub_raw = getattr(h, "time", None)
                if isinstance(pub_raw, datetime):
                    pub = pub_raw.replace(tzinfo=None) if pub_raw.tzinfo else pub_raw
                elif isinstance(pub_raw, str):
                    pub = datetime.fromisoformat(pub_raw).replace(tzinfo=None)
                else:
                    pub = datetime.now(timezone.utc).replace(tzinfo=None)

                if pub < since:
                    continue

                upsert_news(
                    symbol         = symbol,
                    article_id     = article_id,
                    published_at   = pub,
                    headline       = headline,
                    sentiment_score= None,
                )
                results.append({
                    "article_id":      article_id,
                    "published_at":    pub,
                    "headline":        headline,
                    "sentiment_score": None,
                })
            except Exception as exc:
                log.debug("Skipping malformed IBKR headline: %s", exc)

        log.info("Fetched %d articles for %s from IBKR", len(results), symbol)
        return results

    # ── Alpaca source ─────────────────────────────────────────────────────────

    def _get_alpaca_client(self):
        if self._alpaca is None:
            try:
                from alpaca.data.historical import NewsClient as AlpacaNewsClient  # type: ignore
                api_key    = config.alpaca.api_key
                secret_key = config.alpaca.secret_key
                if not api_key or not secret_key:
                    log.debug("Alpaca keys not set; skipping Alpaca news source")
                    return None
                self._alpaca = AlpacaNewsClient(api_key=api_key, secret_key=secret_key)
            except ImportError:
                log.debug("alpaca-py not installed; skipping Alpaca news source")
        return self._alpaca

    def _fetch_from_alpaca(self, symbol: str, since: datetime) -> list[dict]:
        client = self._get_alpaca_client()
        if client is None:
            return []

        try:
            from alpaca.data.requests import NewsRequest  # type: ignore

            since_utc = since.replace(tzinfo=timezone.utc)
            request = NewsRequest(
                symbols = symbol,
                start   = since_utc,
                limit   = config.alpaca.max_articles_per_symbol,
                sort    = "desc",
            )
            response = client.get_news(request)
            # alpaca-py ≥ 0.30 returns a NewsSet whose articles live at
            # response.data["news"]; older versions exposed response.news.
            if hasattr(response, "data") and isinstance(response.data, dict):
                articles = response.data.get("news", [])
            elif hasattr(response, "news"):
                articles = response.news
            else:
                articles = list(response)
        except Exception as exc:
            log.warning("Alpaca news fetch failed for %s: %s", symbol, exc)
            return []

        results: list[dict] = []
        for art in articles:
            try:
                pub = art.created_at
                if hasattr(pub, "tzinfo") and pub.tzinfo is not None:
                    pub = pub.astimezone(timezone.utc).replace(tzinfo=None)

                article_id = str(art.id)
                headline   = art.headline or ""

                upsert_news(
                    symbol          = symbol,
                    article_id      = article_id,
                    published_at    = pub,
                    headline        = headline,
                    sentiment_score = None,
                )
                results.append({
                    "article_id":      article_id,
                    "published_at":    pub,
                    "headline":        headline,
                    "sentiment_score": None,
                })
            except Exception as exc:
                log.debug("Skipping malformed Alpaca article: %s", exc)

        log.info("Fetched %d articles for %s from Alpaca", len(results), symbol)
        return results

    # ── yfinance source ───────────────────────────────────────────────────────

    def _fetch_from_yfinance(self, symbol: str, since: datetime) -> list[dict]:
        """
        Fetch news from yfinance — no API key required.

        yfinance returns the ~10 most recent articles regardless of the `since`
        date, so we filter client-side.  Article IDs are derived from the URL
        to provide a stable deduplication key.
        """
        try:
            import yfinance as yf
        except ImportError:
            log.warning("yfinance not installed; cannot fetch news fallback")
            return []

        try:
            raw_items = yf.Ticker(symbol).news or []
        except Exception as exc:
            log.warning("yfinance news fetch failed for %s: %s", symbol, exc)
            return []

        results: list[dict] = []
        for item in raw_items[:config.alpaca.max_articles_per_symbol]:
            try:
                content = item.get("content", item)

                headline = (
                    content.get("title")
                    or content.get("headline")
                    or ""
                )
                if not headline:
                    continue

                pub_raw = (
                    content.get("pubDate")
                    or content.get("displayTime")
                    or item.get("providerPublishTime")
                )
                if isinstance(pub_raw, int):
                    pub = datetime.fromtimestamp(pub_raw, tz=timezone.utc).replace(tzinfo=None)
                elif isinstance(pub_raw, str):
                    pub = datetime.fromisoformat(
                        pub_raw.replace("Z", "+00:00")
                    ).astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    pub = datetime.now(timezone.utc).replace(tzinfo=None)

                if pub < since:
                    continue

                url = (
                    content.get("canonicalUrl", {}).get("url", "")
                    or content.get("clickThroughUrl", {}).get("url", "")
                    or item.get("link", "")
                    or item.get("url", "")
                )
                article_id = str(item.get("id") or item.get("uuid") or url or headline[:64])[:64]

                upsert_news(
                    symbol          = symbol,
                    article_id      = article_id,
                    published_at    = pub,
                    headline        = headline,
                    sentiment_score = None,
                )
                results.append({
                    "article_id":      article_id,
                    "published_at":    pub,
                    "headline":        headline,
                    "sentiment_score": None,
                })
            except Exception as exc:
                log.debug("Skipping yfinance news item: %s", exc)

        log.info("Fetched %d articles for %s from yfinance", len(results), symbol)
        return results
