"""
XGBoost tabular signal model.

Features: technical indicators from the latest bar + fundamental ratios.
Target:   sign of 5-bar forward return (1 = up, 0 = down).
Output:   predict_proba mapped to [-1, 1] (2*p - 1).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import config
from core.logger import get_logger
from data.fundamentals import FundamentalsClient
from models.base_model import BaseModel

log = get_logger("models.xgboost")

_INDICATOR_FEATURES = [
    "rsi_14", "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_middle", "bb_lower",
    "ema_9", "ema_21", "ema_50",
    "atr_14", "volume_sma_20",
]

_FUNDAMENTAL_FEATURES = [
    "market_cap", "pe_ratio", "forward_pe", "price_to_book",
    "ev_to_ebitda", "revenue_growth", "earnings_growth",
    "profit_margin", "roe", "debt_to_equity", "current_ratio",
    "free_cashflow", "analyst_target",
]

_ALL_FEATURES = _INDICATOR_FEATURES + _FUNDAMENTAL_FEATURES

_FORWARD_BARS = 5


class XGBoostModel(BaseModel):

    def __init__(self, symbol: str = "") -> None:
        cfg = config.ml
        self._symbol   = symbol
        self._params = {
            "n_estimators":    cfg.xgb_n_estimators,
            "max_depth":       cfg.xgb_max_depth,
            "learning_rate":   cfg.xgb_learning_rate,
            "subsample":       cfg.xgb_subsample,
            "colsample_bytree": cfg.xgb_colsample,
            "objective":       "binary:logistic",
            "eval_metric":     "logloss",
        }
        self._model = None
        self._fundamentals = FundamentalsClient()
        self._feature_columns: list[str] = _ALL_FEATURES[:]

    @property
    def name(self) -> str:
        return "xgboost"

    def _build_features(self, df: pd.DataFrame, symbol: str | None = None) -> pd.DataFrame:
        """
        Build a feature matrix from indicator columns + fundamentals.
        Missing columns are filled with 0.
        """
        sym = symbol or self._symbol
        feat = pd.DataFrame(index=df.index)

        for col in _INDICATOR_FEATURES:
            feat[col] = df[col] if col in df.columns else 0.0

        fund_vec = self._fundamentals.get_feature_vector(sym) if sym else {}
        for col in _FUNDAMENTAL_FEATURES:
            feat[col] = fund_vec.get(col, 0.0)

        # Backstop: yfinance can return inf for undefined ratios (e.g. forward P/E
        # with zero forward earnings), and indicator NaN-divisions could also leak
        # non-finite values. XGBoost rejects inf unless `missing=inf` is set; we
        # treat inf the same as NaN — fill with 0.0.
        return feat.replace([np.inf, -np.inf], 0.0).fillna(0.0)

    def _build_labels(self, df: pd.DataFrame) -> pd.Series:
        """Binary label: 1 if close is higher _FORWARD_BARS bars later, else 0."""
        closes = df["Close"] if "Close" in df.columns else pd.Series(dtype=float)
        fwd    = closes.shift(-_FORWARD_BARS)
        return (fwd > closes).astype(int)

    def train(self, train_df: pd.DataFrame) -> None:
        try:
            import xgboost as xgb  # type: ignore
        except ImportError:
            log.error("xgboost not installed")
            return

        X = self._build_features(train_df)
        y = self._build_labels(train_df)

        # Drop rows where forward return is unknowable
        valid = y.dropna().index
        X, y = X.loc[valid], y.loc[valid]

        if len(X) < 20:
            log.warning("XGBoost: too few samples (%d) to train", len(X))
            return

        self._model = xgb.XGBClassifier(**self._params)
        self._model.fit(X, y, verbose=False)

        # Log top-5 feature importances
        imp = pd.Series(
            self._model.feature_importances_,
            index=self._feature_columns,
        ).sort_values(ascending=False)
        log.info("XGBoost top features: %s", imp.head(5).to_dict())

    def predict(self, df: pd.DataFrame) -> float:
        if self._model is None or df.empty:
            return 0.0
        X = self._build_features(df).tail(1)
        prob = float(self._model.predict_proba(X)[0, 1])
        return 2 * prob - 1   # map [0,1] → [-1, 1]

    def evaluate(self, test_df: pd.DataFrame) -> dict:
        if self._model is None or "Close" not in test_df.columns:
            return {"total_return": 0.0, "sharpe_ratio": 0.0}

        X = self._build_features(test_df)
        probs = self._model.predict_proba(X)[:, 1]
        scores = pd.Series(2 * probs - 1, index=test_df.index)
        return self._returns_metrics(scores, test_df["Close"])

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._model:
            self._model.save_model(str(path))
            meta_path = path.with_suffix(".json")
            meta_path.write_text(json.dumps({
                "symbol":           self._symbol,
                "feature_columns":  self._feature_columns,
            }))
            log.info("XGBoost saved to %s", path)

    def load(self, path: str | Path) -> None:
        try:
            import xgboost as xgb  # type: ignore
        except ImportError:
            raise ImportError("xgboost is not installed")

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"No XGBoost model at {path}")

        self._model = xgb.XGBClassifier()
        self._model.load_model(str(path))

        meta_path = path.with_suffix(".json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            self._symbol          = meta.get("symbol", self._symbol)
            self._feature_columns = meta.get("feature_columns", self._feature_columns)

        log.debug("XGBoost loaded from %s", path)
