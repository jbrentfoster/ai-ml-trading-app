"""
LLMNewsAnalyst — local-LLM full-article news extraction (shadow workflow).

Pipeline per article:
    body text ──> Ollama (8B, JSON mode) ──> structured fields
                                              └─> compute_composite_score()  ──> [-1, 1]

Design (see the planning discussion):
  * The LLM only does what it's good at — classify direction / magnitude /
    novelty / horizon, identify entities, write a one-line summary.  It does
    NOT decide the final number.
  * compute_composite_score() turns those fields into the sentiment score of
    record via a transparent, tunable formula, so the dashboard can show
    exactly how the score was reached (sign x magnitude x novelty discount).
  * We also ask the model for its own [-1,1] guess (``llm_direct_score``) and
    store it as a shadow cross-check only — never as the score of record.
  * Attribution: ``primary_entity`` is the company the article is *about*; it
    is frequently NOT the feed-tag symbol (IBKR symbol-tagged news mentions
    other names constantly).  Score attribution keys off this, not the tag.

Nothing here is consumed by signal_runner — outputs land in
``llm_news_analysis`` for the dashboard only.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field

from config.settings import config
from core.logger import get_logger

log = get_logger("models.llm_analyst")

_VALID_EVENT = {"earnings", "guidance", "mgmt_change", "mna", "litigation",
                "regulatory", "product", "analyst", "macro", "other"}
_VALID_DIRECTION = {"bullish", "bearish", "neutral"}
_VALID_HORIZON = {"immediate", "days", "quarter", "longterm"}

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

EXTRACTION_PROMPT = """You are a financial news analyst. Read the article and return ONLY a JSON object with EXACTLY these keys:
- event_type: one of [earnings, guidance, mgmt_change, mna, litigation, regulatory, product, analyst, macro, other]
- direction: one of [bullish, bearish, neutral] — the likely effect on the PRIMARY company's stock
- magnitude: integer 1-5 — how material this is to the primary company's stock (5 = highly material)
- time_horizon: one of [immediate, days, quarter, longterm]
- novelty: integer 1-5 — how new/surprising this is vs already-known (5 = genuinely new information)
- confidence: integer 1-5 — your confidence that you understood the article correctly
- primary_entity: the single company the article is MOST about (the one whose stock is most affected)
- entities: list of all company/person names mentioned
- direct_score: number between -1.0 and 1.0 — your own overall sentiment estimate for the primary company
- summary: one sentence, max 25 words
- rationale: one short phrase explaining the direction/magnitude call

Article:
\"\"\"
{body}
\"\"\"

JSON:"""


@dataclass
class AnalysisResult:
    """Parsed + scored output for one article."""
    event_type: str | None = None
    direction: str | None = None
    magnitude: int | None = None
    time_horizon: str | None = None
    novelty: int | None = None
    confidence: int | None = None
    primary_entity: str | None = None
    entities: list = field(default_factory=list)
    summary: str | None = None
    rationale: str | None = None
    composite_score: float | None = None
    llm_direct_score: float | None = None
    raw_response: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    parse_ok: bool = False


# ── attribution: primary_entity (company name) -> ticker (pure, unit-tested) ──

_NAME_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "group", "holdings", "holding", "technologies",
    "technology", "the", "sa", "nv", "ag", "systems", "international",
    "industries", "enterprises", "labs", "laboratories",
}


def normalize_company_name(s: str | None) -> str:
    """Lowercase, strip punctuation + corporate suffixes, collapse whitespace.
    'Broadcom Inc.' -> 'broadcom'; 'NVIDIA Corporation' -> 'nvidia'."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    toks = [t for t in s.split() if t and t not in _NAME_SUFFIXES]
    return " ".join(toks)


def resolve_ticker(primary_entity: str | None, name_map: dict) -> str | None:
    """Map an LLM ``primary_entity`` (a company NAME) back to a ticker using
    ``name_map`` = {TICKER: [name variants]}.  Returns the ticker or None when
    no known company matches.  A bare ticker mention (``primary_entity=='AVGO'``)
    resolves directly.

    Matching is deliberately conservative to avoid false positives from short
    tickers: an exact (case-insensitive) ticker match, OR a token-set match
    against a real *company name* variant (the bare-ticker variant is never used
    for fuzzy matching, so 'Nvidia' can't match the ETF ticker 'DIA' via a
    substring)."""
    if not primary_entity:
        return None
    direct = primary_entity.strip().upper()
    if direct in name_map:
        return direct
    pe_tokens = set(normalize_company_name(primary_entity).split())
    if not pe_tokens:
        return None
    for ticker, variants in name_map.items():
        for v in variants:
            if v.strip().upper() == ticker:      # skip the bare-ticker variant
                continue
            nv_tokens = set(normalize_company_name(v).split())
            if not nv_tokens:
                continue
            if pe_tokens == nv_tokens:
                return ticker
            # subset match, but only if a substantive (>=4-char) token is shared
            if (pe_tokens <= nv_tokens or nv_tokens <= pe_tokens) and \
               any(len(t) >= 4 for t in (pe_tokens & nv_tokens)):
                return ticker
    return None


def resolve_attribution(primary_entity: str | None, feed_symbol: str,
                        name_map: dict) -> tuple[str | None, bool]:
    """Return (attributed_symbol, mismatch) for one article.

    * attributed_symbol = the ticker the article is *about* (the company whose
      stock the sentiment should attach to), or None when it's some company we
      don't track.
    * mismatch = True when the article is clearly NOT about its feed-tag symbol
      (a different known ticker, or an untracked company that isn't the feed
      company).  These are the rows the headline-only path misattributes."""
    if not primary_entity:
        return feed_symbol, False
    feed = feed_symbol.upper()
    resolved = resolve_ticker(primary_entity, name_map)
    if resolved is not None:
        return resolved, (resolved != feed)
    # Unresolved company: is it at least the feed company under a name we have?
    feed_variants = name_map.get(feed, [feed_symbol])
    matches_feed = resolve_ticker(primary_entity, {feed: feed_variants}) is not None
    return (feed if matches_feed else None), (not matches_feed)


# ── scoring (pure, unit-tested) ───────────────────────────────────────────────

def compute_composite_score(
    direction: str | None,
    magnitude: int | None,
    novelty: int | None,
    novelty_floor: float | None = None,
) -> float | None:
    """
    Deterministic sentiment score in [-1, 1] from the LLM's structured fields.

        sign      = +1 bullish / -1 bearish / 0 neutral
        intensity = magnitude / 5                         in [0.2, 1.0]
        nov_mult  = floor + (1 - floor) * (novelty / 5)   in [floor, 1.0]
        score     = clamp(sign * intensity * nov_mult, -1, 1)

    Already-known news (low novelty) is discounted toward ``novelty_floor``
    (default config.llm.novelty_discount_floor) because it is more likely
    already priced in.  Returns None if direction/magnitude are unusable.
    """
    if novelty_floor is None:
        novelty_floor = config.llm.novelty_discount_floor

    if direction is None:
        return None
    d = direction.strip().lower()
    if d == "neutral":
        return 0.0
    sign = 1.0 if d == "bullish" else (-1.0 if d == "bearish" else None)
    if sign is None:
        return None
    if magnitude is None:
        return None
    try:
        mag = max(1, min(5, int(magnitude)))
    except (TypeError, ValueError):
        return None

    intensity = mag / 5.0
    if novelty is None:
        nov_mult = 1.0
    else:
        try:
            nov = max(1, min(5, int(novelty)))
            nov_mult = novelty_floor + (1.0 - novelty_floor) * (nov / 5.0)
        except (TypeError, ValueError):
            nov_mult = 1.0

    score = sign * intensity * nov_mult
    return max(-1.0, min(1.0, score))


# ── parsing (pure, unit-tested) ───────────────────────────────────────────────

def parse_llm_json(text: str) -> dict | None:
    """Best-effort parse of a model response into a dict.

    Handles markdown ```json fences and trailing prose by extracting the first
    balanced {...} block.  Returns None if no valid JSON object is found."""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    # Fast path
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fallback: grab the first {...} span and try again
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _coerce_int(v, lo: int, hi: int) -> int | None:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _coerce_enum(v, allowed: set, lower: bool = True) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip().lower() if lower else v.strip()
    return s if s in allowed else None


def build_result(parsed: dict | None, novelty_floor: float | None = None) -> AnalysisResult:
    """Validate/coerce a parsed dict into an AnalysisResult + composite score."""
    r = AnalysisResult()
    if not parsed:
        return r  # parse_ok stays False

    r.event_type     = _coerce_enum(parsed.get("event_type"), _VALID_EVENT)
    r.direction      = _coerce_enum(parsed.get("direction"), _VALID_DIRECTION)
    r.time_horizon   = _coerce_enum(parsed.get("time_horizon"), _VALID_HORIZON)
    r.magnitude      = _coerce_int(parsed.get("magnitude"), 1, 5)
    r.novelty        = _coerce_int(parsed.get("novelty"), 1, 5)
    r.confidence     = _coerce_int(parsed.get("confidence"), 1, 5)
    pe = parsed.get("primary_entity")
    r.primary_entity = pe.strip()[:80] if isinstance(pe, str) and pe.strip() else None
    ents = parsed.get("entities")
    r.entities       = [str(e).strip() for e in ents][:25] if isinstance(ents, list) else []
    summ = parsed.get("summary")
    r.summary        = str(summ).strip()[:400] if summ else None
    rat = parsed.get("rationale")
    r.rationale      = str(rat).strip()[:300] if rat else None

    ds = parsed.get("direct_score")
    try:
        r.llm_direct_score = max(-1.0, min(1.0, float(ds))) if ds is not None else None
    except (TypeError, ValueError):
        r.llm_direct_score = None

    r.composite_score = compute_composite_score(
        r.direction, r.magnitude, r.novelty, novelty_floor)
    # parse_ok = we got the fields that actually drive the score of record
    r.parse_ok = r.direction is not None and r.composite_score is not None
    return r


# ── Ollama client ─────────────────────────────────────────────────────────────

class LLMNewsAnalyst:
    """Thin wrapper around the local Ollama HTTP API for news extraction."""

    def __init__(self, model: str | None = None, url: str | None = None,
                 num_predict: int | None = None, timeout_s: int | None = None):
        self.model = model or config.llm.model
        self.url = url or config.llm.ollama_url
        self.num_predict = num_predict or config.llm.num_predict
        self.timeout_s = timeout_s or config.llm.request_timeout_s

    def _generate(self, prompt: str) -> dict:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",                 # constrain to valid JSON (kills the markdown-fence problem)
            "options": {"num_predict": self.num_predict, "temperature": 0},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def analyse(self, body: str) -> AnalysisResult:
        """Run extraction + scoring on one article body. Never raises — on any
        failure returns an AnalysisResult with parse_ok=False so the batch
        continues and the failure is visible in the DB."""
        prompt = EXTRACTION_PROMPT.format(body=body)
        t0 = time.time()
        try:
            resp = self._generate(prompt)
        except Exception as exc:
            log.warning("Ollama generate failed: %s", exc)
            r = AnalysisResult()
            r.duration_ms = int((time.time() - t0) * 1000)
            return r

        wall_ms = int((time.time() - t0) * 1000)
        text = resp.get("response", "") or ""
        result = build_result(parse_llm_json(text))
        result.raw_response = text
        result.prompt_tokens = resp.get("prompt_eval_count", 0) or 0
        result.output_tokens = resp.get("eval_count", 0) or 0
        result.duration_ms = wall_ms
        return result

    def ping(self) -> bool:
        """True if Ollama responds and the model is loadable."""
        try:
            self._generate("Reply with {\"ok\": true}")
            return True
        except Exception as exc:
            log.warning("Ollama ping failed: %s", exc)
            return False
