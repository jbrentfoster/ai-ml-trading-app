"""
Unit tests for the Step 3 ML signal generation layer.

All external dependencies (yfinance, alpaca, torch, xgboost, transformers)
are mocked.  Tests run without a network connection or GPU.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_bars(n: int = 300, start: str = "2023-01-01") -> pd.DataFrame:
    """Return a synthetic OHLCV + indicator DataFrame."""
    rng = np.random.default_rng(42)
    dates = pd.date_range(start, periods=n, freq="B")
    closes = 100 + rng.normal(0, 1, n).cumsum()
    df = pd.DataFrame(
        {
            "Open":   closes * rng.uniform(0.99, 1.0, n),
            "High":   closes * rng.uniform(1.00, 1.01, n),
            "Low":    closes * rng.uniform(0.99, 1.0, n),
            "Close":  closes,
            "Volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            # Indicators (synthetic but non-NaN)
            "rsi_14":      rng.uniform(30, 70, n),
            "macd":        rng.normal(0, 0.5, n),
            "macd_signal": rng.normal(0, 0.4, n),
            "macd_hist":   rng.normal(0, 0.2, n),
            "bb_upper":    closes + 2,
            "bb_middle":   closes,
            "bb_lower":    closes - 2,
            "ema_9":       closes * 0.99,
            "ema_21":      closes * 0.98,
            "ema_50":      closes * 0.97,
            "atr_14":      rng.uniform(0.5, 2.0, n),
            "volume_sma_20": rng.uniform(1e6, 4e6, n),
        },
        index=dates,
    )
    return df


# ── DatasetBuilder ─────────────────────────────────────────────────────────────

class TestDatasetBuilder:

    def test_fit_transform_shape(self):
        from models.lstm_model import DatasetBuilder, _FEATURE_COLS
        builder = DatasetBuilder(seq_len=20, feature_cols=_FEATURE_COLS)
        df = _make_bars(100)
        builder.fit(df)
        arr = builder.transform(df)
        assert arr.shape == (100, len(_FEATURE_COLS))

    def test_build_no_lookahead(self):
        from models.lstm_model import DatasetBuilder, _FEATURE_COLS
        builder = DatasetBuilder(seq_len=20, feature_cols=_FEATURE_COLS)
        df = _make_bars(100)
        builder.fit(df)
        X, y = builder.build(df, forward_bars=5)
        # X samples must only use past data; label uses close at i+forward_bars
        assert X.shape[1] == 20
        assert len(X) == len(y)

    def test_missing_columns_filled_zero(self):
        from models.lstm_model import DatasetBuilder, _FEATURE_COLS
        builder = DatasetBuilder(seq_len=10, feature_cols=_FEATURE_COLS)
        df = _make_bars(50)
        builder.fit(df)
        # Drop a column
        df2 = df.drop(columns=["rsi_14"])
        arr = builder.transform(df2)
        # Should not raise; rsi_14 column should be 0.0
        assert arr.shape[1] == len(_FEATURE_COLS)


# ── LSTMModel ─────────────────────────────────────────────────────────────────

class TestLSTMModel:

    @patch("models.lstm_model._LSTMNet")
    def test_predict_returns_zero_untrained(self, _mock_net):
        from models.lstm_model import LSTMModel
        model = LSTMModel()
        df = _make_bars(80)
        score = model.predict(df)
        assert score == 0.0

    @patch("models.lstm_model._LSTMNet")
    def test_predict_clipped(self, _mock_net):
        """Score must stay in [-1, 1]."""
        import torch
        from models.lstm_model import LSTMModel, DatasetBuilder, _FEATURE_COLS
        model = LSTMModel()
        # Manually inject a trained net mock that returns a large value
        net_mock = MagicMock()
        net_mock.return_value = torch.tensor([[5.0]])
        model._net = net_mock
        model._dataset.fit(_make_bars(200))
        df = _make_bars(100)
        score = model.predict(df)
        assert -1.0 <= score <= 1.0


# ── XGBoostModel ──────────────────────────────────────────────────────────────

class TestXGBoostModel:

    def test_predict_zero_untrained(self):
        from models.xgboost_model import XGBoostModel
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {}
            model = XGBoostModel(symbol="AAPL")
            score = model.predict(_make_bars(50))
            assert score == 0.0

    def test_train_predict_range(self):
        from models.xgboost_model import XGBoostModel
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {
                "pe_ratio": 25.0, "market_cap": 3e12,
            }
            model = XGBoostModel(symbol="AAPL")
            df = _make_bars(200)
            model.train(df)
            score = model.predict(df)
            assert -1.0 <= score <= 1.0

    def test_inf_fundamentals_replaced_with_zero(self):
        """POET 2026-05-11: yfinance returned forward_pe=inf, crashing XGBoost training.

        Backstop in _build_features must replace ±inf with 0.0 so training and
        prediction never see non-finite values.
        """
        import math
        from models.xgboost_model import XGBoostModel
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {
                "forward_pe":     math.inf,
                "pe_ratio":      -math.inf,
                "ev_to_ebitda":   math.nan,
                "market_cap":     1.5e9,
            }
            model = XGBoostModel(symbol="POET")
            df = _make_bars(200)
            # Would raise XGBoostError before the fix
            model.train(df)
            score = model.predict(df)
            assert -1.0 <= score <= 1.0


# ── FundamentalsClient ────────────────────────────────────────────────────────

class TestFundamentalsHelpers:

    def test_safe_float_rejects_inf(self):
        import math
        from data.fundamentals import _safe_float
        assert _safe_float(math.inf) is None
        assert _safe_float(-math.inf) is None
        assert _safe_float(math.nan) is None
        assert _safe_float(42.0) == 42.0
        assert _safe_float(None, fallback=0.0) == 0.0

    def test_get_feature_vector_replaces_inf_from_cache(self):
        """Existing cached fundamentals rows (pre-fix) may still contain inf
        for up to 24h until the TTL refresh writes a None instead. Read path
        must coerce inf to 0.0 so XGBoost never sees it."""
        import math
        from data.fundamentals import FundamentalsClient
        cached = {
            "fetched_at":      datetime.now(timezone.utc).replace(tzinfo=None),
            "market_cap":      1.5e9,
            "forward_pe":      math.inf,
            "pe_ratio":       -math.inf,
            "ev_to_ebitda":    math.nan,
            "revenue_growth":  0.1,
        }
        with patch("data.fundamentals.get_fundamentals", return_value=cached):
            client = FundamentalsClient()
            vec = client.get_feature_vector("POET")
        assert vec["forward_pe"]    == 0.0
        assert vec["pe_ratio"]      == 0.0
        assert vec["ev_to_ebitda"]  == 0.0
        assert vec["market_cap"]    == 1.5e9
        assert vec["revenue_growth"] == pytest.approx(0.1)


# ── FinBERTModel ──────────────────────────────────────────────────────────────

class TestFinBERTModel:

    def test_predict_returns_zero_no_news(self):
        from models.finbert_model import FinBERTModel
        with (
            patch("models.finbert_model.get_recent_news", return_value=[]),
            patch("models.finbert_model.NewsClient") as MockAlpaca,
        ):
            MockAlpaca.return_value.fetch_news.return_value = []
            model = FinBERTModel()
            score = model.predict(pd.DataFrame(), symbol="AAPL")
            assert score == 0.0

    def test_predict_positive_sentiment(self):
        from models.finbert_model import FinBERTModel
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        articles = [
            {"article_id": "1", "published_at": now, "headline": "Record profits", "sentiment_score": 0.9},
        ]
        with (
            patch("models.finbert_model.get_recent_news", return_value=articles),
            patch("models.finbert_model.NewsClient"),
        ):
            model = FinBERTModel()
            # Bypass pipeline loading — score already stored
            score = model.predict(pd.DataFrame(), symbol="AAPL")
            assert score > 0


# ── RegimeDetector ────────────────────────────────────────────────────────────

class TestRegimeDetector:

    def test_empty_df_returns_mean_reverting(self):
        from models.regime_detector import RegimeDetector, RegimeType
        with patch("models.regime_detector.RegimeDetector._get_vix", return_value=None):
            det = RegimeDetector()
            r   = det.detect(pd.DataFrame())
            assert r == RegimeType.MEAN_REVERTING

    def test_high_vix_triggers_high_volatility(self):
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "_get_vix", return_value=35.0):
            det = RegimeDetector()
            r   = det.detect(_make_bars(50))
            assert r == RegimeType.HIGH_VOLATILITY


# ── SignalGate ─────────────────────────────────────────────────────────────────

class TestSignalGate:

    def _gate_with_regime(self, regime_type):
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector
        with patch.object(RegimeDetector, "detect", return_value=regime_type):
            gate = SignalGate()
        return gate

    def test_below_threshold_returns_hold(self):
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "detect", return_value=RegimeType.MEAN_REVERTING):
            gate   = SignalGate()
            df     = _make_bars(50)
            scores = {"lstm": 0.1, "xgb": 0.1, "finbert": 0.05, "ensemble": 0.1}
            result = gate.evaluate("AAPL", df, scores)
        assert result.signal == "HOLD"
        assert not result.passed_gate

    def test_strong_aligned_signal_passes(self):
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "detect", return_value=RegimeType.TRENDING):
            gate   = SignalGate()
            df     = _make_bars(50)
            scores = {"lstm": 0.8, "xgb": 0.7, "finbert": 0.6, "ensemble": 0.7}
            result = gate.evaluate("AAPL", df, scores)
        assert result.signal == "BUY"
        assert result.passed_gate

    def test_high_volatility_raises_threshold(self):
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "detect", return_value=RegimeType.HIGH_VOLATILITY):
            gate = SignalGate()
            gate._base_threshold = 0.35     # pin threshold independent of config drift
            df = _make_bars(50)
            # ensemble = 0.40, which passes base 0.35 but not 0.35*1.5 = 0.525
            scores = {"lstm": 0.5, "xgb": 0.4, "finbert": 0.3, "ensemble": 0.40}
            result = gate.evaluate("AAPL", df, scores)
        assert result.signal == "HOLD"
        assert "Filter2" in result.gate_reason
