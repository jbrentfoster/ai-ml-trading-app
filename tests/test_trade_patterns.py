"""Unit tests for models/trade_patterns.py — the Trade Forensics pattern registry.

Each seed pattern gets a fires / does-not-fire pair built around the case study
it encodes (TEL = LSTM/MACD divergence, UAL = RSI+LSTM extreme, AZN =
sentiment-propped).  Plus the None-safe guard and bucket grouping.
"""

from __future__ import annotations

from models.trade_patterns import (
    PATTERNS,
    BUCKET_DISAGREEMENT,
    BUCKET_EXTENDED,
    EntryContext,
    evaluate,
    group_by_bucket,
)


def _ids(patterns):
    return {p.id for p in patterns}


# A "clean" BUY entry that should trip nothing: moderate scores, mid RSI, MACD
# agrees, well clear of the threshold but not saturated, inside the bands.
def _clean_buy() -> EntryContext:
    return EntryContext(
        direction="BUY",
        lstm=0.55, xgb=0.50, finbert=0.20, ensemble=0.50,
        threshold=0.35, regime="MEAN_REVERTING",
        rsi=55.0, macd=0.40, bb_upper=110.0, bb_lower=90.0, close=100.0,
    )


def test_clean_entry_fires_nothing():
    assert evaluate(_clean_buy()) == []


# ── lstm_macd_divergence (TEL) ─────────────────────────────────────────────────

def test_lstm_macd_divergence_fires_on_buy():
    ctx = EntryContext(direction="BUY", lstm=0.98, macd=-2.76, ensemble=0.45,
                       threshold=0.35, rsi=60, bb_upper=110, close=100,
                       finbert=0.18)
    assert "lstm_macd_divergence" in _ids(evaluate(ctx))


def test_lstm_macd_divergence_silent_when_macd_agrees():
    ctx = _clean_buy()  # lstm 0.55 (<0.7) and macd +0.4
    assert "lstm_macd_divergence" not in _ids(evaluate(ctx))


def test_lstm_macd_divergence_symmetric_for_sell():
    ctx = EntryContext(direction="SELL", lstm=-0.85, macd=1.5, ensemble=-0.5,
                       threshold=0.35, rsi=45, bb_lower=90, close=100, finbert=0.0)
    assert "lstm_macd_divergence" in _ids(evaluate(ctx))


# ── rsi_extreme_lstm (UAL) ─────────────────────────────────────────────────────

def test_rsi_extreme_lstm_fires_when_overbought_and_saturated():
    ctx = EntryContext(direction="BUY", lstm=0.95, macd=0.5, ensemble=0.6,
                       threshold=0.35, rsi=74.0, bb_upper=110, close=105, finbert=0.1)
    assert "rsi_extreme_lstm" in _ids(evaluate(ctx))


def test_rsi_extreme_lstm_silent_below_rsi_threshold():
    ctx = EntryContext(direction="BUY", lstm=0.95, macd=0.5, ensemble=0.6,
                       threshold=0.35, rsi=65.0, bb_upper=110, close=105, finbert=0.1)
    assert "rsi_extreme_lstm" not in _ids(evaluate(ctx))


# ── price_outside_bands ────────────────────────────────────────────────────────

def test_price_outside_bands_fires_above_upper():
    ctx = EntryContext(direction="BUY", lstm=0.95, macd=0.5, ensemble=0.6,
                       threshold=0.35, rsi=60, bb_upper=99.0, close=100.0, finbert=0.1)
    assert "price_outside_bands" in _ids(evaluate(ctx))


def test_price_outside_bands_silent_inside_band():
    ctx = _clean_buy()  # close 100 < bb_upper 110, and lstm 0.55 anyway
    assert "price_outside_bands" not in _ids(evaluate(ctx))


# ── sentiment_propped (AZN) ────────────────────────────────────────────────────

def test_sentiment_propped_fires_when_finbert_carries_bearish_lstm():
    ctx = EntryContext(direction="BUY", lstm=-0.40, finbert=0.55, ensemble=0.10,
                       threshold=0.35, macd=0.1, rsi=55, bb_upper=110, close=100)
    assert "sentiment_propped" in _ids(evaluate(ctx))


def test_sentiment_propped_silent_when_lstm_agrees():
    ctx = _clean_buy()  # lstm positive
    assert "sentiment_propped" not in _ids(evaluate(ctx))


# ── low_conviction_entry ───────────────────────────────────────────────────────

def test_low_conviction_fires_just_over_threshold():
    ctx = EntryContext(direction="BUY", lstm=0.6, macd=0.4, ensemble=0.37,
                       threshold=0.35, rsi=55, bb_upper=110, close=100, finbert=0.1)
    assert "low_conviction_entry" in _ids(evaluate(ctx))


def test_low_conviction_silent_when_strong():
    ctx = EntryContext(direction="BUY", lstm=0.6, macd=0.4, ensemble=0.62,
                       threshold=0.35, rsi=55, bb_upper=110, close=100, finbert=0.1)
    assert "low_conviction_entry" not in _ids(evaluate(ctx))


# ── high_vol_entry ─────────────────────────────────────────────────────────────

def test_high_vol_entry_fires_on_regime():
    ctx = EntryContext(direction="BUY", lstm=0.6, macd=0.4, ensemble=0.62,
                       threshold=0.525, regime="HIGH_VOLATILITY",
                       rsi=55, bb_upper=110, close=100, finbert=0.1)
    assert "high_vol_entry" in _ids(evaluate(ctx))


# ── multi-fire + grouping ──────────────────────────────────────────────────────

def test_multiple_patterns_can_fire_together():
    # TEL-divergence + overbought + price-outside-band + squeaker all at once.
    ctx = EntryContext(direction="BUY", lstm=0.98, macd=-1.0, ensemble=0.37,
                       threshold=0.35, regime="HIGH_VOLATILITY",
                       rsi=75, bb_upper=99, close=100, finbert=0.1)
    fired = _ids(evaluate(ctx))
    assert {"lstm_macd_divergence", "rsi_extreme_lstm",
            "price_outside_bands", "low_conviction_entry",
            "high_vol_entry"} <= fired


def test_grouping_orders_disagreement_before_extended():
    ctx = EntryContext(direction="BUY", lstm=0.98, macd=-1.0, ensemble=0.6,
                       threshold=0.35, rsi=75, bb_upper=99, close=100, finbert=0.1)
    grouped = group_by_bucket(evaluate(ctx))
    keys = list(grouped.keys())
    assert keys.index(BUCKET_DISAGREEMENT) < keys.index(BUCKET_EXTENDED)


# ── None-safe guard ────────────────────────────────────────────────────────────

def test_none_inputs_do_not_crash_and_do_not_fire():
    # Every numeric field None — comparisons would raise TypeError without the
    # _safe_detect guard.  high_vol_entry still evaluates (string compare),
    # low_conviction can't (None arithmetic) so it must not fire.
    ctx = EntryContext(direction="BUY", regime="TRENDING")
    fired = evaluate(ctx)            # must not raise
    assert "low_conviction_entry" not in _ids(fired)
    assert "lstm_macd_divergence" not in _ids(fired)


def test_seed_ids_are_unique():
    ids = [p.id for p in PATTERNS]
    assert len(ids) == len(set(ids))
