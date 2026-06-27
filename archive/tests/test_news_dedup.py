"""
Unit tests for data/news_dedup.py — read-time event clustering.

Pins the behaviour that motivated it: the four near-identical "Marvell surges
on Nvidia endorsement" articles (2026-06-02) collapsing into one event with a
single representative score, while genuinely distinct stories stay separate.
"""

from datetime import datetime

from data.news_dedup import cluster_news_events, jaccard, _tokens


def _art(article_id, primary, headline, summary, day="2026-06-01",
         confidence=3, prompt_tokens=700, score=0.0):
    return {
        "article_id": article_id,
        "symbol": "NVDA",
        "primary_entity": primary,
        "published_at": datetime.fromisoformat(f"{day}T12:00:00"),
        "headline": headline,
        "summary": summary,
        "confidence": confidence,
        "prompt_tokens": prompt_tokens,
        "composite_score": score,
    }


class TestJaccard:
    def test_identical(self):
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint(self):
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_empty(self):
        assert jaccard(set(), {"a"}) == 0.0

    def test_partial(self):
        assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3

    def test_tokens_drop_stopwords(self):
        # "on"/"the" are stopwords; "marvell"/"surges" survive
        assert _tokens("Marvell surges on the news") == {"marvell", "surges", "news"}


class TestEventClustering:
    def _marvell_batch(self):
        # the real 2026-06-02 pattern: same event, 4 articles, inconsistent scores
        return [
            _art("a1", "Marvell Technology",
                 "Marvell stock surges on Nvidia CEO's comments",
                 "Marvell stock surges on Nvidia CEO's endorsement at Computex.",
                 confidence=3, score=0.00),
            _art("a2", "Marvell",
                 "Marvell stock surges after Nvidia's endorsement at Computex",
                 "Marvell stock surges after Nvidia's endorsement at Computex event.",
                 confidence=5, prompt_tokens=900, score=0.90),
            _art("a3", "Marvell Technology",
                 "Marvell stock surges on Jensen Huang's comments",
                 "Marvell stock surges on Nvidia CEO Jensen Huang's comments at Computex.",
                 confidence=2, score=0.00),
            _art("a4", "Marvell Technology",
                 "Marvell stock surges on Nvidia CEO's endorsement, market value up",
                 "Marvell stock surges on Nvidia's endorsement at Computex, market value rises.",
                 confidence=4, score=0.90),
        ]

    def test_marvell_collapses_to_one_event(self):
        out = cluster_news_events(self._marvell_batch())
        event_ids = {a["event_id"] for a in out}
        assert len(event_ids) == 1
        assert all(a["event_size"] == 4 for a in out)

    def test_exactly_one_representative(self):
        out = cluster_news_events(self._marvell_batch())
        reps = [a for a in out if a["is_representative"]]
        assert len(reps) == 1
        # highest confidence (5) wins -> article a2
        assert reps[0]["article_id"] == "a2"

    def test_event_score_is_mean_of_all_reads(self):
        # [0.00, 0.90, 0.00, 0.90] -> mean +0.45, on EVERY member row
        out = cluster_news_events(self._marvell_batch())
        assert all(abs(a["event_score"] - 0.45) < 1e-9 for a in out)
        # representative's own composite is NOT the event score (that was the bug)
        rep = next(a for a in out if a["is_representative"])
        assert rep["composite_score"] == 0.90  # a2's own read
        assert abs(rep["event_score"] - 0.45) < 1e-9

    def test_event_score_ignores_none_scores(self):
        batch = self._marvell_batch()
        batch[0]["composite_score"] = None   # a parse-failed read
        out = cluster_news_events(batch)
        # mean of [0.90, 0.00, 0.90] = 0.60
        assert all(abs(a["event_score"] - 0.60) < 1e-9 for a in out)

    def test_different_company_separate_event(self):
        batch = self._marvell_batch() + [
            _art("b1", "Broadcom",
                 "Broadcom heads for record close",
                 "Broadcom rises on Google and Marvell news.", confidence=4),
        ]
        out = cluster_news_events(batch)
        assert len({a["event_id"] for a in out}) == 2

    def test_digests_cluster_by_headline_not_feed_symbol(self):
        # Same "Morning Report" digest broadcast under two different feed tags
        # (NXPI and AMD) must collapse into ONE event keyed on the digest, not
        # split per feed symbol nor merge into a real ticker's bucket.
        def _digest(feed):
            return {
                "article_id": f"dig-{feed}", "symbol": feed,
                "primary_entity": "ACEL", "attributed_symbol": None,
                "attribution_status": "digest", "is_digest": True,
                "published_at": datetime.fromisoformat("2026-06-03T08:44:00"),
                "headline": "Substantial Insider Sales: Morning Report",
                "summary": "", "confidence": 5, "prompt_tokens": 700,
                "composite_score": -0.5,
            }
        out = cluster_news_events([_digest("NXPI"), _digest("AMD")])
        assert len({a["event_id"] for a in out}) == 1
        assert all(a["event_id"].startswith("digest:") for a in out)
        assert all(a["event_size"] == 2 for a in out)

    def test_digest_does_not_merge_with_real_ticker_event(self):
        # A digest whose primary_entity is "ACEL" must NOT merge with a genuine
        # story about a company that resolves to the same name token.
        digest = {
            "article_id": "dig1", "symbol": "NXPI", "primary_entity": "Accel",
            "attributed_symbol": None, "attribution_status": "digest",
            "is_digest": True,
            "published_at": datetime.fromisoformat("2026-06-03T08:00:00"),
            "headline": "Substantial Insider Sales: Morning Report",
            "summary": "", "confidence": 5, "prompt_tokens": 700,
            "composite_score": -0.5,
        }
        real = {
            "article_id": "real1", "symbol": "ACEL", "primary_entity": "Accel",
            "attributed_symbol": "ACEL", "attribution_status": "matched",
            "is_digest": False,
            "published_at": datetime.fromisoformat("2026-06-03T09:00:00"),
            "headline": "Accel beats earnings", "summary": "",
            "confidence": 4, "prompt_tokens": 700, "composite_score": 0.6,
        }
        out = cluster_news_events([digest, real])
        assert len({a["event_id"] for a in out}) == 2

    def test_same_company_same_day_merges_known_tradeoff(self):
        # KNOWN TRADE-OFF: two genuinely different same-day stories about one
        # company merge into one event.  Accepted because text similarity proved
        # unreliable on real data (intra-event < inter-event Jaccard) and
        # re-report inflation is the bigger problem.  event_size keeps it visible.
        batch = [
            _art("c1", "Apple", "Apple unveils new iPhone",
                 "Apple announces the latest iPhone at its event.", confidence=3),
            _art("c2", "Apple", "Apple faces EU antitrust fine",
                 "Apple hit with a major EU antitrust penalty over App Store.", confidence=3),
        ]
        out = cluster_news_events(batch)
        assert len({a["event_id"] for a in out}) == 1
        assert all(a["event_size"] == 2 for a in out)

    def test_attributed_symbol_is_the_grouping_key(self):
        # different primary_entity TEXT but same resolved ticker -> one event
        batch = [
            dict(_art("e1", "Marvell", "Marvell up", "Marvell rises."),
                 attributed_symbol="MRVL"),
            dict(_art("e2", "Marvell Technology Inc", "Marvell climbs", "Marvell gains."),
                 attributed_symbol="MRVL"),
        ]
        out = cluster_news_events(batch)
        assert len({a["event_id"] for a in out}) == 1

    def test_different_days_separate(self):
        batch = [
            _art("d1", "Apple", "Apple rises", "Apple rises on news.", day="2026-06-01"),
            _art("d2", "Apple", "Apple rises", "Apple rises on news.", day="2026-06-02"),
        ]
        out = cluster_news_events(batch)
        assert len({a["event_id"] for a in out}) == 2

    def test_preserves_all_rows(self):
        batch = self._marvell_batch()
        out = cluster_news_events(batch)
        assert len(out) == len(batch)
        assert {a["article_id"] for a in out} == {"a1", "a2", "a3", "a4"}

    def test_empty_input(self):
        assert cluster_news_events([]) == []
