"""
FinBERT news-sentiment model.

Uses ProsusAI/finbert (HuggingFace) to score financial news headlines.
Scores are aggregated with exponential time-decay (half-life = 24 h by default).

Returns 0.0 when:
  - No news is available within the staleness window
  - The HuggingFace pipeline cannot be loaded
"""

from __future__ import annotations

import logging
import math
import os
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Silence HuggingFace startup noise BEFORE transformers/huggingface_hub are
# imported anywhere.  Without these, FinBERT load spams the daily log with:
#   * tqdm "Loading weights: ... 201/201" progress bar
#   * "BertForSequenceClassification LOAD REPORT" verbose model-load summary
#   * "You are sending unauthenticated requests to the HF Hub" warning
# All three go to stdout/stderr and end up in the daily batch-file log.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# The "unauthenticated requests to the HF Hub" notice is emitted via
# `warnings.warn` (not env-var controlled) AND its `huggingface_hub.utils._http`
# logger when warnings are captured.  Silence both paths.
warnings.filterwarnings("ignore", message=r".*unauthenticated requests to the HF Hub.*")
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)

import pandas as pd

from config.settings import config
from core.logger import get_logger
from data.news_client import NewsClient
from data.database import get_recent_news, upsert_news
from models.base_model import BaseModel

log = get_logger("models.finbert")


class FinBERTModel(BaseModel):
    # Class-level pipeline cache — shared across all instances so the ~400 MB
    # model is loaded exactly once per process regardless of how many symbols
    # are processed.
    _shared_pipeline = None
    _shared_model_name: str = ""

    def __init__(self) -> None:
        cfg = config.ml
        self._model_name     = cfg.finbert_model_name
        self._half_life_h    = cfg.sentiment_half_life_hours
        self._staleness_days = cfg.sentiment_staleness_days
        self._news_client    = NewsClient()

    @property
    def name(self) -> str:
        return "finbert"

    def _get_pipeline(self):
        if (FinBERTModel._shared_pipeline is None
                or FinBERTModel._shared_model_name != self._model_name):
            try:
                from transformers import pipeline  # type: ignore
                from transformers import logging as hf_logging  # type: ignore
                hf_logging.set_verbosity_error()
                FinBERTModel._shared_pipeline = pipeline(
                    "text-classification",
                    model=self._model_name,
                    top_k=None,
                    # Model is already cached locally; skip the HuggingFace
                    # version-check API call that triggers 429 rate limit errors.
                    local_files_only=True,
                )
                FinBERTModel._shared_model_name = self._model_name
                log.info("FinBERT pipeline loaded from %s", self._model_name)
            except Exception:
                # local_files_only fails on first-ever download — fall back to
                # online load so the model can be fetched the first time.
                try:
                    from transformers import pipeline  # type: ignore
                    FinBERTModel._shared_pipeline = pipeline(
                        "text-classification",
                        model=self._model_name,
                        top_k=None,
                    )
                    FinBERTModel._shared_model_name = self._model_name
                    log.info("FinBERT pipeline loaded from %s (online)", self._model_name)
                except Exception as exc:
                    log.warning("Could not load FinBERT: %s", exc)
        return FinBERTModel._shared_pipeline

    def _score_headline(self, headline: str) -> float:
        """
        Run FinBERT on a single headline.
        Returns a score in [-1, 1]:  positive → bullish, negative → bearish.
        Returns 0.0 on failure.
        """
        pipe = self._get_pipeline()
        if pipe is None:
            return 0.0
        try:
            results = pipe(headline[:512])[0]   # truncate to BERT max
            label_scores = {r["label"].lower(): r["score"] for r in results}
            return float(
                label_scores.get("positive", 0.0) - label_scores.get("negative", 0.0)
            )
        except Exception as exc:
            log.debug("FinBERT scoring failed: %s", exc)
            return 0.0

    def _decay_weight(self, published_at: datetime, now: datetime) -> float:
        """Exponential decay weight based on article age."""
        age_hours = (now - published_at).total_seconds() / 3600
        return math.exp(-math.log(2) * age_hours / self._half_life_h)

    @staticmethod
    def _coerce_published_at(value) -> datetime | None:
        """Coerce a news article's ``published_at`` to a tz-naive datetime.

        NewsClient has three providers (IBKR / Alpaca / yfinance) that don't
        all return the same type — IBKR returns ``datetime``, Alpaca returns
        ISO strings, yfinance may return ``pd.Timestamp``. Without this
        coercion, the ``published_at <= now`` filter raises ``TypeError``
        when comparing a string to a datetime, or quietly mis-orders a
        tz-aware timestamp against the tz-naive DB convention.

        Returns ``None`` when the value can't be parsed.
        """
        if value is None:
            return None
        try:
            ts = pd.to_datetime(value, errors="coerce")
        except Exception:
            return None
        if ts is pd.NaT or pd.isna(ts):
            return None
        # Strip timezone — the rest of the pipeline (DB, ``now``) is tz-naive UTC.
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.tz_convert(None)
        return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

    def _aggregate_sentiment(self, symbol: str, as_of: datetime | None = None) -> float:
        """
        Fetch and score recent news for `symbol`.
        Returns a weighted average sentiment in [-1, 1], or 0.0 if no news.

        Parameters
        ----------
        as_of :
            Reference timestamp for the news window.  Defaults to now.
            Pass a historical bar timestamp during walk-forward to avoid
            lookahead: only articles published in [as_of-staleness, as_of]
            are used.  When as_of is in the past and the DB has no articles
            for that window, returns 0.0 (no score rather than wrong score).
        """
        now    = (as_of if as_of is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
        cutoff = now - timedelta(days=self._staleness_days)

        articles = get_recent_news(symbol, since=cutoff)

        # Normalise published_at across all three NewsClient providers before
        # any timestamp comparison.  Drop unparseable rows rather than letting
        # them poison the weighted average.
        normalised = []
        for a in articles:
            pub = self._coerce_published_at(a.get("published_at"))
            if pub is None:
                continue
            a["published_at"] = pub
            normalised.append(a)
        articles = normalised

        # Filter out articles published after as_of (no lookahead during backtest)
        if as_of is not None:
            articles = [a for a in articles if a["published_at"] <= now]

        if not articles and as_of is None:
            # Only attempt a live fetch when running in real-time (not backtest)
            articles = self._news_client.fetch_news(symbol, days_back=self._staleness_days)
            # Live-fetch path goes around get_recent_news, so it hasn't been
            # normalised yet — apply the same coercion.
            normalised = []
            for a in articles:
                pub = self._coerce_published_at(a.get("published_at"))
                if pub is None:
                    continue
                a["published_at"] = pub
                normalised.append(a)
            articles = normalised

        if not articles:
            log.debug("No news for %s within %d days - returning 0.0", symbol, self._staleness_days)
            return 0.0

        weighted_sum = 0.0
        weight_total = 0.0
        pipe = self._get_pipeline()

        for art in articles:
            pub = art["published_at"]   # already coerced above

            # Score only if not yet stored
            score = art.get("sentiment_score")
            if score is None and pipe is not None:
                score = self._score_headline(art["headline"])
                # Update cache
                upsert_news(
                    symbol=symbol,
                    article_id=art["article_id"],
                    published_at=pub,
                    headline=art["headline"],
                    sentiment_score=score,
                )

            if score is None:
                score = 0.0

            w = self._decay_weight(pub, now)
            weighted_sum += score * w
            weight_total += w

        if weight_total == 0:
            return 0.0
        return max(-1.0, min(1.0, weighted_sum / weight_total))

    # ── BaseModel interface ────────────────────────────────────────────────────

    @staticmethod
    def is_available_for_date(date: datetime, cutoff: datetime | None = None) -> bool:
        """
        Return False when `date` precedes the resolved news cutoff.

        Walk-forward callers use this to decide whether to suppress FinBERT
        scoring for a given test window. ``cutoff`` (when provided) is used
        directly; otherwise ``config.ml.news_available_from`` is consulted.
        Returns True when no cutoff is configured at either site.
        """
        naf = cutoff if cutoff is not None else config.ml.news_available_from
        if naf is None:
            return True
        # Normalise to naive UTC for comparison (both dates are naive in the DB)
        if hasattr(date, "tzinfo") and date.tzinfo is not None:
            date = date.replace(tzinfo=None)
        if hasattr(naf, "tzinfo") and naf.tzinfo is not None:
            naf = naf.replace(tzinfo=None)
        return date >= naf

    def train(self, train_df: pd.DataFrame) -> None:
        """FinBERT is pre-trained; no fine-tuning is performed."""
        log.info("FinBERT: using pre-trained weights (no fine-tuning)")

    def predict(self, df: pd.DataFrame, symbol: str = "",
                as_of: datetime | None = None) -> float:
        """
        Return the time-decayed aggregate sentiment score for `symbol`.
        `df` is accepted for API compatibility but not directly used.

        Parameters
        ----------
        as_of :
            When provided, only news published on or before this timestamp is
            used.  Pass the bar timestamp during walk-forward to prevent
            lookahead bias.
        """
        if not symbol:
            return 0.0
        return self._aggregate_sentiment(symbol, as_of=as_of)

    def evaluate(self, test_df: pd.DataFrame) -> dict:
        """
        FinBERT evaluation: returns a fixed stub since sentiment is not
        directly backtestable without per-bar news archives.
        """
        return {"total_return": 0.0, "sharpe_ratio": 0.0}

    def save(self, path: str | Path) -> None:
        """Nothing to persist — model weights are on HuggingFace Hub."""

    def load(self, path: str | Path) -> None:
        """Nothing to restore — pipeline is loaded lazily."""
