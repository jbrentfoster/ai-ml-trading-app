"""
Unit tests for models/llm_analyst.py — the pure scoring + JSON-parsing logic.

No Ollama / network needed; the HTTP client (LLMNewsAnalyst.analyse) is not
exercised here (it's I/O glue).  These pin the two things that are easy to get
wrong and that drive the score of record: the composite-score formula and the
defensive JSON parsing.
"""

import math

import pytest

from models.llm_analyst import (
    compute_composite_score,
    parse_llm_json,
    build_result,
    normalize_company_name,
    resolve_ticker,
    resolve_attribution,
)


# ── attribution resolver ──────────────────────────────────────────────────────

_NAME_MAP = {
    "NVDA": ["NVDA", "NVIDIA Corporation"],
    "AVGO": ["AVGO", "Broadcom Inc."],
    "AAPL": ["AAPL", "Apple Inc."],
}


class TestAttribution:
    def test_normalize_strips_suffixes(self):
        assert normalize_company_name("Broadcom Inc.") == "broadcom"
        assert normalize_company_name("NVIDIA Corporation") == "nvidia"
        assert normalize_company_name("Apple Inc.") == "apple"

    def test_resolve_bare_ticker(self):
        assert resolve_ticker("AVGO", _NAME_MAP) == "AVGO"

    def test_resolve_company_name(self):
        assert resolve_ticker("Broadcom", _NAME_MAP) == "AVGO"
        assert resolve_ticker("NVIDIA Corporation", _NAME_MAP) == "NVDA"

    def test_resolve_parenthetical_ticker(self):
        # LLM appended the ticker in parentheses; it's tracked -> use it
        nm = {**_NAME_MAP, "TMUS": ["TMUS", "T-Mobile US, Inc. Common Stock"]}
        assert resolve_ticker("T-Mobile US Inc (TMUS)", nm) == "TMUS"

    def test_parenthetical_ticker_untracked_falls_through(self):
        # parenthetical ticker we don't track -> not returned (stays None here)
        assert resolve_ticker("Obscure Co (ZZZZ)", _NAME_MAP) is None

    def test_resolve_unknown_company(self):
        assert resolve_ticker("Some Private Startup", _NAME_MAP) is None

    def test_resolve_empty(self):
        assert resolve_ticker(None, _NAME_MAP) is None
        assert resolve_ticker("", _NAME_MAP) is None

    def test_mismatch_known_other_ticker(self):
        # NVDA-tagged article about Broadcom -> attributed AVGO, mismatch True
        attributed, mismatch = resolve_attribution("Broadcom", "NVDA", _NAME_MAP)
        assert attributed == "AVGO"
        assert mismatch is True

    def test_no_mismatch_when_about_feed_company(self):
        attributed, mismatch = resolve_attribution("NVIDIA Corporation", "NVDA", _NAME_MAP)
        assert attributed == "NVDA"
        assert mismatch is False

    def test_untracked_company_is_mismatch_with_none(self):
        attributed, mismatch = resolve_attribution("Private Startup LLC", "NVDA", _NAME_MAP)
        assert attributed is None
        assert mismatch is True

    def test_no_primary_entity_no_mismatch(self):
        attributed, mismatch = resolve_attribution(None, "NVDA", _NAME_MAP)
        assert attributed == "NVDA"
        assert mismatch is False


# ── compute_composite_score ───────────────────────────────────────────────────

class TestCompositeScore:
    def test_neutral_is_zero(self):
        assert compute_composite_score("neutral", 5, 5) == 0.0

    def test_bullish_sign_positive_bearish_negative(self):
        assert compute_composite_score("bullish", 3, 3) > 0
        assert compute_composite_score("bearish", 3, 3) < 0

    def test_max_bullish_high_novelty(self):
        # sign=+1, intensity=5/5=1.0, nov_mult = 0.5 + 0.5*(5/5)=1.0 -> 1.0
        assert compute_composite_score("bullish", 5, 5, novelty_floor=0.5) == 1.0

    def test_novelty_discount_applies(self):
        # magnitude fixed; lower novelty -> smaller magnitude of score
        hi = compute_composite_score("bullish", 4, 5, novelty_floor=0.5)
        lo = compute_composite_score("bullish", 4, 1, novelty_floor=0.5)
        assert hi > lo > 0

    def test_known_value_decomposition(self):
        # bearish, mag 4, novelty 3, floor 0.5:
        #   intensity = 4/5 = 0.8
        #   nov_mult  = 0.5 + 0.5*(3/5) = 0.8
        #   score     = -1 * 0.8 * 0.8 = -0.64
        s = compute_composite_score("bearish", 4, 3, novelty_floor=0.5)
        assert math.isclose(s, -0.64, abs_tol=1e-9)

    def test_floor_changes_result(self):
        # floor=0.0 makes low-novelty news score near zero
        s = compute_composite_score("bullish", 5, 1, novelty_floor=0.0)
        # nov_mult = 0.0 + 1.0*(1/5) = 0.2 ; score = 1*1.0*0.2 = 0.2
        assert math.isclose(s, 0.2, abs_tol=1e-9)

    def test_missing_novelty_defaults_to_full_weight(self):
        # no novelty -> nov_mult = 1.0
        assert compute_composite_score("bullish", 5, None) == 1.0

    def test_clamped_to_unit_range(self):
        s = compute_composite_score("bullish", 5, 5, novelty_floor=0.9)
        assert -1.0 <= s <= 1.0

    def test_unusable_inputs_return_none(self):
        assert compute_composite_score(None, 3, 3) is None
        assert compute_composite_score("bullish", None, 3) is None
        assert compute_composite_score("garbage", 3, 3) is None

    def test_out_of_range_magnitude_clamped(self):
        # magnitude 9 clamps to 5
        assert compute_composite_score("bullish", 9, 5, novelty_floor=0.5) == \
               compute_composite_score("bullish", 5, 5, novelty_floor=0.5)


# ── parse_llm_json ─────────────────────────────────────────────────────────────

class TestParseJSON:
    def test_plain_json(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced(self):
        assert parse_llm_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_no_lang(self):
        assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_trailing_prose(self):
        assert parse_llm_json('{"a": 1}\nHope that helps!') == {"a": 1}

    def test_garbage_returns_none(self):
        assert parse_llm_json("not json at all") is None
        assert parse_llm_json("") is None

    def test_json_array_returns_none(self):
        # we require a dict, not a bare list
        assert parse_llm_json("[1, 2, 3]") is None


# ── build_result (validation + coercion + scoring) ────────────────────────────

class TestBuildResult:
    def _good(self):
        return {
            "event_type": "earnings", "direction": "bullish", "magnitude": 4,
            "time_horizon": "days", "novelty": 3, "confidence": 4,
            "primary_entity": "Broadcom",
            "entities": ["Broadcom", "Nvidia"], "direct_score": 0.6,
            "summary": "Broadcom beat estimates.", "rationale": "earnings beat",
        }

    def test_happy_path(self):
        r = build_result(self._good(), novelty_floor=0.5)
        assert r.parse_ok is True
        assert r.direction == "bullish"
        assert r.magnitude == 4
        assert r.primary_entity == "Broadcom"
        assert r.entities == ["Broadcom", "Nvidia"]
        assert r.llm_direct_score == pytest.approx(0.6)
        assert r.composite_score == pytest.approx(0.64)  # 0.8 * 0.8

    def test_none_input_not_ok(self):
        r = build_result(None)
        assert r.parse_ok is False
        assert r.composite_score is None

    def test_invalid_enum_coerced_to_none(self):
        d = self._good()
        d["event_type"] = "explosion"   # not a valid enum
        r = build_result(d)
        assert r.event_type is None
        assert r.parse_ok is True        # direction still valid -> score still computes

    def test_missing_direction_not_ok(self):
        d = self._good()
        del d["direction"]
        r = build_result(d)
        assert r.parse_ok is False
        assert r.composite_score is None

    def test_string_magnitude_coerced(self):
        d = self._good()
        d["magnitude"] = "4"
        r = build_result(d, novelty_floor=0.5)
        assert r.magnitude == 4
        assert r.composite_score == pytest.approx(0.64)

    def test_bad_direct_score_nulled_but_row_ok(self):
        d = self._good()
        d["direct_score"] = "very bullish"
        r = build_result(d)
        assert r.llm_direct_score is None
        assert r.parse_ok is True
