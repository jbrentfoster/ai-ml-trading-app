"""
Trade-entry pattern flags for the Page 10 Trade Forensics panel.

A *declarative registry* of named patterns that may have fired at a trade's
entry — model disagreements, over-extension, low-conviction squeakers, etc.
Each pattern is data (an :class:`TradePattern`), not an inline ``if`` buried in
the dashboard page.  The panel calls :func:`evaluate` against an
:class:`EntryContext` assembled from the trade's signal-log row + indicators at
entry, and renders whichever patterns fired, grouped by ``bucket``.

**Why a registry?**  We expect the pattern catalogue to grow as more case
studies surface recurring entry mistakes (the seed list below encodes the
TEL / UAL / AZN findings).  Adding pattern #20 should be appending one entry to
``PATTERNS`` — never editing the dashboard.  A new pattern only touches plumbing
when it needs an input :class:`EntryContext` doesn't yet carry; add a field
there and populate it once in the query layer.

**Provisional thresholds.**  The numeric cutoffs (lstm>0.7, rsi>=70, …) encode
findings that are still hypothesised, not proven across many trades — see
``docs/findings/`` and the relevant case studies.  They are deliberately easy to
tweak in one place.  v1 evaluates flags at *read time* only; nothing is
persisted.  The stable ``id`` on each pattern is what makes a future
"which patterns precede losses?" aggregation purely additive.

Pure functions; unit-tested in tests/test_trade_patterns.py.  No DB, no config
import — the caller passes the effective gate threshold in via EntryContext so
this module stays free of settings coupling.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

# ── Buckets ───────────────────────────────────────────────────────────────────
# Bucket strings group patterns for display (and, later, for "which family of
# mistakes costs us most" aggregation).  Keep them stable — they double as keys.

BUCKET_DISAGREEMENT = "Model disagreement"
BUCKET_EXTENDED     = "Overbought / extended"
BUCKET_CONVICTION   = "Low conviction"
BUCKET_REGIME       = "Regime"

# Display order for grouping (patterns in unknown buckets sort last, alpha).
_BUCKET_ORDER = [
    BUCKET_DISAGREEMENT,
    BUCKET_EXTENDED,
    BUCKET_CONVICTION,
    BUCKET_REGIME,
]

# Severity is advisory styling only ("info" < "caution" < "warning").
SEVERITY_INFO    = "info"
SEVERITY_CAUTION = "caution"
SEVERITY_WARNING = "warning"


# ── Context ─────────────────────────────────────────────────────────────────--

@dataclass(frozen=True)
class EntryContext:
    """Everything a pattern rule might inspect about a trade's entry.

    Assembled once by the query layer from the entry-anchored ``signal_log`` row
    plus the ``indicator_snapshots`` row at (or just before) the entry bar.  Any
    field may be ``None`` when the underlying data wasn't cached — rules guard
    against that via :func:`evaluate` (a ``None`` comparison never crashes the
    panel, it just means the rule doesn't fire).

    ``threshold`` is the *effective* (regime-adjusted) gate threshold the signal
    actually had to clear — passed in by the caller so this module needn't know
    about config or regime multipliers.
    """

    direction: str                      # "BUY" | "SELL"
    lstm:      float | None = None
    xgb:       float | None = None
    finbert:   float | None = None
    ensemble:  float | None = None
    threshold: float | None = None      # effective regime-adjusted gate threshold
    regime:    str | None   = None      # RegimeType.value, e.g. "TRENDING"
    rsi:       float | None = None
    macd:      float | None = None
    bb_upper:  float | None = None
    bb_lower:  float | None = None
    close:     float | None = None

    @property
    def is_buy(self) -> bool:
        return (self.direction or "").upper() == "BUY"

    @property
    def is_sell(self) -> bool:
        return (self.direction or "").upper() == "SELL"


@dataclass(frozen=True)
class TradePattern:
    """One declarative entry-pattern flag.

    ``detect`` returns True when the pattern fired for the given context;
    ``explain`` produces the human sentence shown when it does.  Both receive
    the same :class:`EntryContext`.
    """

    id:       str                               # stable key (survives label edits)
    label:    str                               # short human title
    bucket:   str                               # one of the BUCKET_* constants
    severity: str                               # SEVERITY_*
    detect:   Callable[[EntryContext], bool]
    explain:  Callable[[EntryContext], str]


# ── Seed pattern catalogue ─────────────────────────────────────────────────────
# Each entry encodes a finding from a case study; thresholds are provisional.

PATTERNS: list[TradePattern] = [
    # ── Model disagreement ──────────────────────────────────────────────────
    TradePattern(
        id="lstm_macd_divergence",
        label="LSTM vs MACD direction conflict",
        bucket=BUCKET_DISAGREEMENT,
        severity=SEVERITY_WARNING,
        detect=lambda c: (
            (c.is_buy  and c.lstm > 0.7  and c.macd < 0) or
            (c.is_sell and c.lstm < -0.7 and c.macd > 0)
        ),
        explain=lambda c: (
            f"LSTM {c.lstm:+.2f} (strong {'bull' if c.is_buy else 'bear'}) but "
            f"MACD {c.macd:+.2f} points the other way — the TEL pattern "
            "(2026-05 case study): price model and momentum disagreed at entry."
        ),
    ),
    TradePattern(
        id="sentiment_propped",
        label="FinBERT propping a contrary price model",
        bucket=BUCKET_DISAGREEMENT,
        severity=SEVERITY_CAUTION,
        detect=lambda c: (
            (c.is_buy  and c.finbert > 0.3  and c.lstm < 0 and c.ensemble > 0) or
            (c.is_sell and c.finbert < -0.3 and c.lstm > 0 and c.ensemble < 0)
        ),
        explain=lambda c: (
            f"FinBERT {c.finbert:+.2f} carried the ensemble {c.ensemble:+.2f} "
            f"over the gate while LSTM {c.lstm:+.2f} disagreed — the AZN family "
            "(sentiment overriding a bearish/bullish price model)."
        ),
    ),
    # ── Overbought / extended ──────────────────────────────────────────────--
    TradePattern(
        id="rsi_extreme_lstm",
        label="RSI extreme + saturated LSTM",
        bucket=BUCKET_EXTENDED,
        severity=SEVERITY_CAUTION,
        detect=lambda c: (
            (c.is_buy  and c.rsi >= 70 and c.lstm > 0.9) or
            (c.is_sell and c.rsi <= 30 and c.lstm < -0.9)
        ),
        explain=lambda c: (
            f"RSI {c.rsi:.0f} ({'overbought' if c.is_buy else 'oversold'}) with "
            f"LSTM saturated at {c.lstm:+.2f} — the UAL pattern: entering an "
            "already-stretched move."
        ),
    ),
    TradePattern(
        id="price_outside_bands",
        label="Price outside Bollinger band",
        bucket=BUCKET_EXTENDED,
        severity=SEVERITY_CAUTION,
        detect=lambda c: (
            (c.is_buy  and c.close > c.bb_upper and c.lstm > 0.9) or
            (c.is_sell and c.close < c.bb_lower and c.lstm < -0.9)
        ),
        explain=lambda c: (
            f"Close {c.close:.2f} {'above upper' if c.is_buy else 'below lower'} "
            f"BB ({(c.bb_upper if c.is_buy else c.bb_lower):.2f}) with LSTM "
            f"{c.lstm:+.2f} — extended entry, prone to mean reversion."
        ),
    ),
    # ── Low conviction ──────────────────────────────────────────────────────
    TradePattern(
        id="low_conviction_entry",
        label="Squeaker — barely cleared the gate",
        bucket=BUCKET_CONVICTION,
        severity=SEVERITY_INFO,
        detect=lambda c: (abs(c.ensemble) - c.threshold) < 0.05,
        explain=lambda c: (
            f"Ensemble |{c.ensemble:+.2f}| cleared the effective threshold "
            f"{c.threshold:.2f} by only {abs(c.ensemble) - c.threshold:+.2f} — "
            "a low-conviction entry, not a strong signal."
        ),
    ),
    # ── Regime ──────────────────────────────────────────────────────────────
    TradePattern(
        id="high_vol_entry",
        label="Entered in a high-volatility regime",
        bucket=BUCKET_REGIME,
        severity=SEVERITY_INFO,
        detect=lambda c: (c.regime or "").upper() == "HIGH_VOLATILITY",
        explain=lambda c: (
            "Regime was HIGH_VOLATILITY at entry — the gate threshold was raised "
            "1.5×, so this signal was comparatively strong, but the environment "
            "was volatile."
        ),
    ),
]


# ── Evaluation ──────────────────────────────────────────────────────────────--

def _safe_detect(pattern: TradePattern, ctx: EntryContext) -> bool:
    """Run a pattern's detector, treating any missing input as 'did not fire'.

    Rules compare floats that may be ``None`` (uncached indicator, missing
    score).  In Python ``None > 0`` raises ``TypeError``; we swallow that (and
    any other arithmetic error from absent data) and report False rather than
    crash the whole panel for one unmapped field.
    """
    try:
        return bool(pattern.detect(ctx))
    except (TypeError, ValueError):
        return False


def evaluate(ctx: EntryContext) -> list[TradePattern]:
    """Return every seed pattern that fired for this entry context.

    Order is by bucket (``_BUCKET_ORDER``, then alphabetical for any bucket not
    in the list), preserving catalogue order within a bucket — so the caller can
    group without re-sorting.
    """
    fired = [p for p in PATTERNS if _safe_detect(p, ctx)]
    return sorted(fired, key=_bucket_sort_key)


def group_by_bucket(patterns: list[TradePattern]) -> "OrderedDict[str, list[TradePattern]]":
    """Group an (already evaluated) pattern list into an ordered bucket→list map."""
    grouped: "OrderedDict[str, list[TradePattern]]" = OrderedDict()
    for p in sorted(patterns, key=_bucket_sort_key):
        grouped.setdefault(p.bucket, []).append(p)
    return grouped


def _bucket_sort_key(p: TradePattern) -> tuple[int, str]:
    try:
        idx = _BUCKET_ORDER.index(p.bucket)
    except ValueError:
        idx = len(_BUCKET_ORDER)        # unknown buckets sort last
    return (idx, p.bucket)
