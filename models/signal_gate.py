"""
Signal gate — three sequential filters that must all pass before a signal
is emitted as actionable.

Filter 1 — Threshold:
    |ensemble_score| >= base_threshold (default 0.35)

Filter 2 — Regime-adjusted threshold:
    HIGH_VOLATILITY regime scales by ``high_vol_threshold_multiplier`` (1.5).
    TRENDING scales by ``trending_threshold_multiplier`` (1.2 — stricter,
        because XGBoost is mean-reversion-biased and emits unreliable SELLs
        when indicators are at extremes; pair with the XGBoost downweight
        in EnsembleModel.predict).
    MEAN_REVERTING: base threshold used unchanged.

Filter 3 — 2-of-3 model confirmation:
    At least `min_confirmations` (default 2) of the 3 sub-models must
    agree on direction with the ensemble.

If all three filters pass, the gate returns a BUY or SELL signal.
Otherwise it returns HOLD with a reason string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from config.settings import config
from core.logger import get_logger
from models.regime_detector import RegimeDetector, RegimeType

log = get_logger("models.signal_gate")


@dataclass
class SignalResult:
    symbol:        str
    bar_timestamp: datetime
    generated_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    # Sub-model scores
    lstm_score:    float = 0.0
    xgb_score:     float = 0.0
    finbert_score: float = 0.0

    # Ensemble
    ensemble_score: float = 0.0
    regime:         RegimeType = RegimeType.MEAN_REVERTING

    # Gate decision
    signal:      str  = "HOLD"      # "BUY" | "SELL" | "HOLD"
    passed_gate: bool = False
    gate_reason: str  = ""


class SignalGate:

    def __init__(self) -> None:
        cfg = config.ml
        self._base_threshold  = cfg.signal_threshold
        self._min_confirm     = cfg.signal_confirmation
        self._regime_detector = RegimeDetector()

    def evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        scores: dict[str, float],
    ) -> SignalResult:
        """
        Apply the three-filter gate and return a SignalResult.

        `scores` must contain keys: lstm, xgb, finbert, ensemble.
        `df`     is used by the regime detector (needs OHLCV columns).
        """
        regime    = scores.get("regime") or self._regime_detector.detect(df)
        ensemble  = scores.get("ensemble", 0.0)
        bar_ts    = df.index[-1].to_pydatetime() if hasattr(df.index[-1], "to_pydatetime") else df.index[-1]

        result = SignalResult(
            symbol         = symbol,
            bar_timestamp  = bar_ts,
            lstm_score     = scores.get("lstm", 0.0),
            xgb_score      = scores.get("xgb", 0.0),
            finbert_score  = scores.get("finbert", 0.0),
            ensemble_score = ensemble,
            regime         = regime,
        )

        # ── Filter 1: base threshold ──────────────────────────────────────────
        if abs(ensemble) < self._base_threshold:
            result.gate_reason = (
                f"Filter1 fail: |{ensemble:.3f}| < threshold {self._base_threshold:.3f}"
            )
            return result

        # ── Filter 2: regime-adjusted threshold ───────────────────────────────
        adjusted = self._adjusted_threshold(regime)
        if abs(ensemble) < adjusted:
            result.gate_reason = (
                f"Filter2 fail: |{ensemble:.3f}| < regime-adjusted threshold "
                f"{adjusted:.3f} ({regime.value})"
            )
            return result

        # ── Filter 3: model confirmation ──────────────────────────────────────
        direction = 1 if ensemble > 0 else -1
        individual_scores = [scores.get("lstm", 0.0), scores.get("xgb", 0.0), scores.get("finbert", 0.0)]
        agreements = sum(1 for s in individual_scores if s != 0 and (s > 0) == (direction > 0))

        if agreements < self._min_confirm:
            result.gate_reason = (
                f"Filter3 fail: only {agreements}/{len(individual_scores)} models confirm "
                f"direction (need {self._min_confirm})"
            )
            return result

        # ── All filters passed ────────────────────────────────────────────────
        result.signal      = "BUY" if direction > 0 else "SELL"
        result.passed_gate = True
        result.gate_reason = (
            f"Passed all filters — {regime.value}, "
            f"{agreements}/{len(individual_scores)} confirm, "
            f"score={ensemble:.3f}"
        )
        log.info(
            "[%s] %s signal generated — score=%.3f, regime=%s",
            symbol, result.signal, ensemble, regime.value,
        )
        return result

    def _adjusted_threshold(self, regime: RegimeType) -> float:
        cfg = config.ml
        if regime == RegimeType.HIGH_VOLATILITY:
            return self._base_threshold * cfg.high_vol_threshold_multiplier
        if regime == RegimeType.TRENDING:
            return self._base_threshold * cfg.trending_threshold_multiplier
        return self._base_threshold
