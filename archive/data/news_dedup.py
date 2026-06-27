"""
News event de-duplication for the LLM analyst (shadow workflow).

The same real-world event is reported many times — DJ multi-part fragments,
multiple outlets, re-runs — and the 8B scores near-identical inputs
inconsistently (observed 2026-06-02: four "Marvell surges on Nvidia
endorsement" articles scored +0.00, +0.90, +0.00, +0.90).  Left unclustered,
a symbol's average sentiment is dominated by how many times a story was
re-reported, not by how bullish it is.

This module clusters scored articles into EVENTS at read time (nothing is
re-scored; every raw row is preserved — matches the Page 10 dedup philosophy).

Approach (cheap, no ML):
  1. event = (resolved ticker [attributed_symbol], calendar day).  Falls back to
     normalized primary_entity, then the feed symbol, when no ticker resolved.
  2. pick a representative per event = max(confidence, then completeness
     [prompt_tokens], then recency) — the score of record for that event.

**Why not text similarity?**  An earlier version split each (entity, day) group
further by Jaccard similarity of headline+summary tokens.  Real data (2026-06-02)
killed it: the four Marvell articles had intra-event Jaccard as low as 0.14,
while Marvell-vs-Broadcom (a *different* event) was 0.19 — higher than some
same-event pairs.  Short, reworded headlines share generic chip-sector vocabulary
("stock", "surges", "Nvidia", "Computex") that bleeds across genuinely different
stories, so no threshold separates them.  The resolved ticker is the reliable
signal; entity+day grouping is what the data actually needs.  Known trade-off:
two genuinely different same-day stories about one company merge into one event —
far less harmful than the 4x re-report inflation we're fixing, and ``event_size``
+ the score spread keep over-merges visible on the dashboard.  (``jaccard`` /
``_tokens`` are retained for a possible future *body-level* similarity pass.)

Pure functions; unit-tested in tests/test_news_dedup.py.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from models.llm_analyst import ATTR_DIGEST, normalize_company_name

# light stopword set so "surges ON x" vs "surges AFTER x" don't drag similarity down
_STOP = {
    "the", "a", "an", "on", "in", "of", "to", "and", "but", "for", "with",
    "after", "as", "at", "its", "it", "is", "are", "was", "were", "by", "from",
    "that", "this", "amid", "while", "over", "up", "down", "new",
}


def _tokens(text: str | None) -> set:
    """Lowercase alphanumeric tokens (len>=2), stopwords removed."""
    if not text:
        return set()
    out = set()
    for raw in str(text).lower().split():
        tok = "".join(ch for ch in raw if ch.isalnum())
        if len(tok) >= 2 and tok not in _STOP:
            out.add(tok)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _day_key(published_at) -> str:
    if published_at is None:
        return "na"
    try:
        if isinstance(published_at, str):
            return published_at[:10]
        return published_at.strftime("%Y-%m-%d")
    except Exception:
        return "na"


def _rep_sortkey(a: dict):
    pub = a.get("published_at")
    if not isinstance(pub, datetime):
        pub = datetime.min
    return (a.get("confidence") or 0, a.get("prompt_tokens") or 0, pub)


def _event_key(a: dict) -> str:
    """The strong grouping signal: digests cluster by headline (they have no
    single subject — keep them out of any ticker's bucket); otherwise resolved
    ticker, else normalized company name, else the feed symbol."""
    if a.get("is_digest") or a.get("attribution_status") == ATTR_DIGEST:
        return "digest:" + (normalize_company_name(a.get("headline")) or "na")
    att = a.get("attributed_symbol")
    if att:
        return str(att).upper()
    pe = normalize_company_name(a.get("primary_entity"))
    if pe:
        return pe
    return (a.get("symbol") or "na").lower()


def cluster_news_events(articles: list[dict]) -> list[dict]:
    """Augment each article dict with ``event_id``, ``event_size`` and
    ``is_representative``.  Event = (resolved ticker / entity, calendar day).
    Input dicts need: symbol, attributed_symbol, primary_entity, published_at,
    confidence, prompt_tokens.  Returns NEW dicts (inputs untouched); order is
    not preserved (caller should re-sort)."""
    groups: dict = defaultdict(list)
    for a in articles:
        groups[(_event_key(a), _day_key(a.get("published_at")))].append(a)

    out: list[dict] = []
    for (key, day), members in groups.items():
        event_id = f"{key}|{day}"
        rep = max(members, key=_rep_sortkey)
        # Event score of record = MEAN of all member composite scores (every
        # read counts; the representative is only for DISPLAY of headline/text).
        scores = [a.get("composite_score") for a in members
                  if a.get("composite_score") is not None]
        event_score = sum(scores) / len(scores) if scores else None
        for a in members:
            a2 = dict(a)
            a2["event_id"] = event_id
            a2["event_size"] = len(members)
            a2["is_representative"] = (a is rep)
            a2["event_score"] = event_score
            out.append(a2)
    return out
