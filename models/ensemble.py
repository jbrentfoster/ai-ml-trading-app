"""
Dynamic ensemble that combines LSTM, XGBoost, and FinBERT scores.

Weight adjustment rules:
  - After each evaluation period, nudge weights 10 % toward the best model
  - Floor each weight at 0.10 before re-normalising
  - Persist every rebalance to ensemble_weight_history
"""

from __future__ import annotations

import pandas as pd

from config.settings import config
from core.logger import get_logger
from data.database import log_ensemble_weights
from models.base_model import BaseModel
from models.finbert_model import FinBERTModel
from models.lstm_model import LSTMModel
from models.xgboost_model import XGBoostModel

log = get_logger("models.ensemble")

_NUDGE    = None   # resolved from config at init
_FLOOR    = None
_MODELS   = ("lstm", "xgb", "finbert")


class EnsembleModel:
    """
    Weighted linear combination of three models.

    Usage:
        ens = EnsembleModel(symbol="AAPL")
        ens.train(train_df)
        score = ens.predict(df)      # float in [-1, 1]
        ens.rebalance(eval_results)  # dict: {"lstm": metrics, ...}
    """

    def __init__(self, symbol: str = "") -> None:
        cfg = config.ml
        self._symbol   = symbol
        self._nudge    = cfg.ensemble_nudge
        self._floor    = cfg.ensemble_weight_floor

        self.weights: dict[str, float] = {
            "lstm":    cfg.ensemble_lstm_weight,
            "xgb":     cfg.ensemble_xgb_weight,
            "finbert": cfg.ensemble_finbert_weight,
        }

        self._lstm    = LSTMModel()
        self._xgb     = XGBoostModel(symbol=symbol)
        self._finbert = FinBERTModel()

    # ── Training ──────────────────────────────────────────────────────────────

    def train(self, train_df: pd.DataFrame) -> None:
        log.info("Ensemble training LSTM ...")
        self._lstm.train(train_df)

        log.info("Ensemble training XGBoost ...")
        self._xgb.train(train_df)

        log.info("Ensemble training FinBERT (pre-trained, no fine-tuning) ...")
        self._finbert.train(train_df)

        log.info("Ensemble training complete.  Weights: %s", self.weights)

    # ── Inference ─────────────────────────────────────────────────────────────

    def predict(self, df: pd.DataFrame, suppress_finbert: bool = False,
                as_of=None) -> dict[str, float]:
        """
        Return a dict with individual model scores and the weighted ensemble:
            {"lstm": float, "xgb": float, "finbert": float, "ensemble": float}
        All scores are in [-1, 1].

        Parameters
        ----------
        suppress_finbert :
            Force finbert_score=0.0 and redistribute its weight to LSTM/XGBoost.
            Used for walk-forward windows that predate news_available_from.
        as_of :
            Bar timestamp to pass to FinBERT so only news published on or before
            this date is used.  Prevents lookahead bias during walk-forward.
        """
        lstm_score = self._lstm.predict(df)
        xgb_score  = self._xgb.predict(df)

        if suppress_finbert:
            finbert_score = 0.0
            half = self.weights["finbert"] / 2.0
            w_lstm    = self.weights["lstm"]    + half
            w_xgb     = self.weights["xgb"]     + half
            w_finbert = 0.0
        else:
            finbert_score = self._finbert.predict(df, symbol=self._symbol, as_of=as_of)
            w_lstm    = self.weights["lstm"]
            w_xgb     = self.weights["xgb"]
            w_finbert = self.weights["finbert"]

        ensemble = w_lstm * lstm_score + w_xgb * xgb_score + w_finbert * finbert_score

        return {
            "lstm":     lstm_score,
            "xgb":      xgb_score,
            "finbert":  finbert_score,
            "ensemble": max(-1.0, min(1.0, ensemble)),
        }

    def evaluate(self, test_df: pd.DataFrame) -> dict[str, dict]:
        """Evaluate each sub-model on `test_df`.  Returns per-model metrics."""
        return {
            "lstm":    self._lstm.evaluate(test_df),
            "xgb":     self._xgb.evaluate(test_df),
            "finbert": self._finbert.evaluate(test_df),
        }

    # ── Weight management ─────────────────────────────────────────────────────

    def rebalance(self, eval_results: dict[str, dict],
                  finbert_coverage: float = 1.0) -> None:
        """
        Rebalance ensemble weights after a walk-forward fold.

        Two adjustments are made:

        1. LSTM vs XGBoost competition — nudge weight toward whichever had the
           higher Sharpe ratio.  FinBERT is excluded because its evaluate() is a
           stub (sentiment can't be backtested like a price model).

        2. FinBERT coverage scaling — FinBERT's weight is set to
           ``configured_weight × coverage`` where coverage is the fraction of
           test-window bars that had a non-zero sentiment score (0 = no news,
           1 = every bar had news).  The uncovered share is redistributed to
           LSTM and XGBoost proportionally.  Coverage is always computed fresh
           from the configured baseline so adjustments don't compound across folds.

        Parameters
        ----------
        eval_results :
            {model_name: {"sharpe_ratio": float, ...}}
        finbert_coverage :
            Fraction of test-window bars with non-zero FinBERT scores [0, 1].
        """
        price_models = ["lstm", "xgb"]
        sharpes = {
            m: eval_results.get(m, {}).get("sharpe_ratio", 0.0)
            for m in price_models
        }
        best  = max(sharpes, key=sharpes.get)  # type: ignore[arg-type]
        other = [m for m in price_models if m != best][0]
        log.info("Ensemble rebalance - best price model: %s (Sharpe=%.3f)", best, sharpes[best])

        # Step 1: nudge between LSTM and XGBoost
        transfer = min(self._nudge, self.weights[other] - self._floor)
        self.weights[best]  = min(1.0, self.weights[best]  + transfer)
        self.weights[other] = max(self._floor, self.weights[other] - transfer)

        # Step 2: scale FinBERT weight by news coverage.
        # Always compute from the configured baseline to prevent drift across folds.
        coverage = max(0.0, min(1.0, finbert_coverage))
        self.weights["finbert"] = config.ml.ensemble_finbert_weight * coverage
        log.info(
            "FinBERT coverage=%.0f%% -> weight %.1f%% (configured base %.1f%%)",
            coverage * 100,
            self.weights["finbert"] * 100,
            config.ml.ensemble_finbert_weight * 100,
        )

        self._normalise_weights()
        log.info("Updated ensemble weights: %s", self.weights)
        log_ensemble_weights(
            lstm=self.weights["lstm"],
            xgb=self.weights["xgb"],
            finbert=self.weights["finbert"],
            trigger="rebalance",
        )

    def _normalise_weights(self) -> None:
        """Apply floor then re-normalise so weights sum to 1."""
        for m in _MODELS:
            self.weights[m] = max(self._floor, self.weights[m])
        total = sum(self.weights.values())
        for m in _MODELS:
            self.weights[m] = self.weights[m] / total

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, directory: str) -> None:
        from pathlib import Path
        base = Path(directory)
        self._lstm.save(base / "lstm.pt")
        self._xgb.save(base / "xgb.ubj")
        log.info("Ensemble models saved to %s", base)

    def load(self, directory: str) -> None:
        from pathlib import Path
        base = Path(directory)
        self._lstm.load(base / "lstm.pt")
        self._xgb.load(base / "xgb.ubj")
        log.info("Ensemble models loaded from %s", base)
