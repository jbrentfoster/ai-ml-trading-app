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

    def test_extreme_finite_value_clamped(self):
        """A finite-but-huge fundamental (e.g. ev_to_ebitda for a company with
        near-zero EBITDA) hits the same gradient-index check as inf:
        `Input data contains 'inf' or a value too large`. Backstop must clamp
        any magnitude > _MAX_ABS_FEATURE_VALUE (1e10) to 0."""
        from models.xgboost_model import XGBoostModel
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {
                "ev_to_ebitda":   1.5e18,   # finite but absurd
                "forward_pe":    -9.99e15,  # finite but absurd
                "market_cap":     1.5e9,    # legitimate mega-cap
            }
            model = XGBoostModel(symbol="POET")
            df = _make_bars(200)
            # Would raise XGBoostError before the clamp was added
            model.train(df)
            score = model.predict(df)
            assert -1.0 <= score <= 1.0

    def test_build_features_returns_finite_bounded_values(self):
        """Direct check on _build_features: post-sanitisation, every cell must
        be finite and within the clamp range."""
        import math
        import numpy as np
        from models.xgboost_model import XGBoostModel
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {
                "ev_to_ebitda": 1e20,
                "forward_pe":   math.inf,
                "pe_ratio":     math.nan,
                "market_cap":   1.5e9,
            }
            model = XGBoostModel(symbol="POET")
            df = _make_bars(50)
            feat = model._build_features(df)
            arr = feat.to_numpy()
            assert np.isfinite(arr).all(), "feature matrix must be all-finite"
            assert (np.abs(arr) <= XGBoostModel._MAX_ABS_FEATURE_VALUE).all(), (
                "feature magnitudes must respect _MAX_ABS_FEATURE_VALUE clamp"
            )


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

    def test_coerce_published_at_handles_all_provider_types(self):
        """NewsClient providers return mixed types — IBKR datetime, Alpaca ISO
        string, yfinance pd.Timestamp.  Coercion must handle all three plus
        None / NaT / unparseable garbage without raising."""
        from datetime import datetime as _dt
        import pandas as _pd
        from models.finbert_model import FinBERTModel
        coerce = FinBERTModel._coerce_published_at

        # datetime (tz-naive)        — pass through
        naive = _dt(2026, 5, 12, 14, 0, 0)
        assert coerce(naive) == naive

        # ISO string                 — parse
        result = coerce("2026-05-12T14:00:00")
        assert result == _dt(2026, 5, 12, 14, 0, 0)
        assert result.tzinfo is None

        # pd.Timestamp (tz-naive)    — convert to datetime
        ts = _pd.Timestamp("2026-05-12 14:00:00")
        assert coerce(ts) == _dt(2026, 5, 12, 14, 0, 0)

        # pd.Timestamp (tz-aware UTC) — strip tz, keep wall-clock
        ts_utc = _pd.Timestamp("2026-05-12 14:00:00", tz="UTC")
        result = coerce(ts_utc)
        assert result == _dt(2026, 5, 12, 14, 0, 0)
        assert result.tzinfo is None

        # ISO string with timezone   — strip tz
        result = coerce("2026-05-12T14:00:00+00:00")
        assert result == _dt(2026, 5, 12, 14, 0, 0)
        assert result.tzinfo is None

        # None / NaT / garbage       — return None, don't raise
        assert coerce(None) is None
        assert coerce(_pd.NaT) is None
        assert coerce("not a date") is None

    def test_aggregate_sentiment_filters_string_published_at_without_typeerror(self):
        """Regression: pre-fix, an Alpaca-style ISO string in published_at
        would raise `TypeError: '<=' not supported between str and datetime`
        in the as_of filter.  Coercion must convert it before the comparison."""
        from models.finbert_model import FinBERTModel
        as_of = datetime(2026, 5, 12, 14, 0, 0)
        articles = [
            # IBKR-style: native datetime
            {"article_id": "1", "published_at": datetime(2026, 5, 12, 10, 0, 0),
             "headline": "OK", "sentiment_score": 0.5},
            # Alpaca-style: ISO string (the failure mode the audit flagged)
            {"article_id": "2", "published_at": "2026-05-12T11:00:00",
             "headline": "OK", "sentiment_score": 0.3},
            # yfinance-style: pd.Timestamp
            {"article_id": "3", "published_at": pd.Timestamp("2026-05-12 12:00:00"),
             "headline": "OK", "sentiment_score": 0.4},
            # Should be filtered out as future (after as_of)
            {"article_id": "4", "published_at": "2026-05-13T10:00:00",
             "headline": "Future", "sentiment_score": -0.9},
            # Should be filtered out as garbage
            {"article_id": "5", "published_at": "not a date",
             "headline": "Bad", "sentiment_score": -0.9},
        ]
        with (
            patch("models.finbert_model.get_recent_news", return_value=articles),
            patch("models.finbert_model.NewsClient"),
        ):
            model = FinBERTModel()
            # Must not raise TypeError; future + garbage rows dropped, so the
            # weighted average should be positive (all three valid scores >0).
            score = model.predict(pd.DataFrame(), symbol="AAPL", as_of=as_of)
        assert 0 < score <= 1.0


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

    def test_adx_uses_wilder_smoothing(self):
        """Pin the smoothing factor at alpha = 1/period (Wilder), not 2/(N+1).

        Before the 2026-05-12 fix, ``_compute_adx`` used
        ``ewm(span=period, adjust=False)`` — alpha=2/15≈0.133 for period=14,
        roughly twice Wilder's alpha=1/14≈0.071.  This test computes the
        expected ADX via the Wilder formula and asserts ``_compute_adx``
        matches.  If someone reverts to span-based EMA the assertion fails.
        """
        from models.regime_detector import RegimeDetector
        df = _make_bars(120)
        period = 14
        alpha = 1.0 / period

        high, low, close = df["High"], df["Low"], df["Close"]
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        dm_p = (high - high.shift(1)).clip(lower=0)
        dm_n = (low.shift(1) - low).clip(lower=0)
        dm_p_z = dm_p.where(dm_p > dm_n, 0)
        dm_n_z = dm_n.where(dm_n > dm_p, 0)
        atr  = tr.ewm(alpha=alpha, adjust=False).mean()
        di_p = 100 * dm_p_z.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, float("nan"))
        di_m = 100 * dm_n_z.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, float("nan"))
        dx   = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, float("nan"))
        expected = float(dx.ewm(alpha=alpha, adjust=False).mean().iloc[-1])

        actual = RegimeDetector._compute_adx(df, period=period)
        assert actual is not None
        assert actual == pytest.approx(expected, rel=1e-9)

    def test_adx_strong_trend_exceeds_threshold(self):
        """A clean monotonic uptrend should produce ADX > 25 (TRENDING)."""
        from models.regime_detector import RegimeDetector
        n = 80
        idx = pd.date_range("2023-01-01", periods=n, freq="B")
        closes = pd.Series(np.linspace(100, 200, n), index=idx)
        df = pd.DataFrame({
            "High":  closes + 0.5,
            "Low":   closes - 0.5,
            "Close": closes,
        }, index=idx)
        adx = RegimeDetector._compute_adx(df, period=14)
        assert adx is not None
        assert adx > 25, f"Strong trend should yield ADX > 25, got {adx:.2f}"


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

    def test_trending_raises_threshold(self):
        # Pre-fix the TRENDING multiplier was 0.9 (more permissive) — letting
        # XGBoost-driven SELLs slip through in trending markets. The new
        # default is 1.2 (stricter), pairing with the XGBoost weight halving
        # in EnsembleModel.predict.
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "detect", return_value=RegimeType.TRENDING):
            gate = SignalGate()
            gate._base_threshold = 0.35
            df = _make_bars(50)
            # ensemble = 0.40 passes base 0.35 but not 0.35*1.2 = 0.42
            scores = {"lstm": 0.5, "xgb": 0.4, "finbert": 0.3, "ensemble": 0.40}
            result = gate.evaluate("AAPL", df, scores)
        assert result.signal == "HOLD"
        assert "Filter2" in result.gate_reason

    def test_signal_gate_reuses_regime_from_scores(self):
        # EnsembleModel.predict now returns the regime in its scores dict so
        # the gate doesn't re-detect. When ``scores["regime"]`` is present the
        # gate must use it rather than calling RegimeDetector again.
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        with patch.object(RegimeDetector, "detect") as detect:
            detect.return_value = RegimeType.MEAN_REVERTING
            gate = SignalGate()
            detect.reset_mock()
            df = _make_bars(50)
            scores = {
                "lstm": 0.8, "xgb": 0.7, "finbert": 0.6, "ensemble": 0.7,
                "regime": RegimeType.MEAN_REVERTING,
            }
            result = gate.evaluate("AAPL", df, scores)
        assert result.signal == "BUY"
        assert result.regime == RegimeType.MEAN_REVERTING
        detect.assert_not_called()


# ── EnsembleModel.predict regime adjustment ────────────────────────────────────

class TestEnsemblePredictRegimeAdjust:

    def _make_ensemble(self, lstm_score, xgb_score, finbert_score, regime):
        from models.ensemble import EnsembleModel
        from models.regime_detector import RegimeDetector

        ens = EnsembleModel.__new__(EnsembleModel)
        ens._symbol = "AAPL"
        ens._nudge  = 0.10
        ens._floor  = 0.10
        ens.weights = {"lstm": 0.40, "xgb": 0.35, "finbert": 0.25}
        ens._lstm    = MagicMock(); ens._lstm.predict.return_value    = lstm_score
        ens._xgb     = MagicMock(); ens._xgb.predict.return_value     = xgb_score
        ens._finbert = MagicMock(); ens._finbert.predict.return_value = finbert_score
        ens._regime_detector = MagicMock(spec=RegimeDetector)
        ens._regime_detector.detect.return_value = regime
        return ens

    def test_trending_halves_xgb_contribution(self):
        from models.regime_detector import RegimeType

        # All three component scores at +1 means the unweighted ensemble is +1.
        # With baseline weights (0.40, 0.35, 0.25), ensemble = 1.0 regardless.
        # The interesting test: XGB strongly negative, LSTM/FinBERT positive.
        # Pre-fix: ensemble = 0.40*0.5 + 0.35*(-1.0) + 0.25*0.5 = -0.025
        # Post-fix (TRENDING, xgb_mult=0.5): xgb weight halves to 0.175, the
        # other 0.175 redistributes proportionally to lstm (0.40/0.65) and
        # finbert (0.25/0.65). New weights ~ (0.508, 0.175, 0.317) → ensemble =
        # 0.508*0.5 + 0.175*(-1.0) + 0.317*0.5 = +0.238 (positive!)
        ens = self._make_ensemble(0.5, -1.0, 0.5, RegimeType.TRENDING)
        out = ens.predict(_make_bars(50))
        assert out["xgb"] == -1.0
        assert out["lstm"] == 0.5
        assert out["finbert"] == 0.5
        assert out["ensemble"] > 0.20    # flipped from negative to positive
        assert out["regime"] == RegimeType.TRENDING

    def test_mean_reverting_leaves_weights_alone(self):
        from models.regime_detector import RegimeType
        ens = self._make_ensemble(0.5, -1.0, 0.5, RegimeType.MEAN_REVERTING)
        out = ens.predict(_make_bars(50))
        # Baseline weights: 0.40*0.5 + 0.35*(-1.0) + 0.25*0.5 = -0.025
        assert abs(out["ensemble"] - (-0.025)) < 1e-6
        assert out["regime"] == RegimeType.MEAN_REVERTING

    def test_caller_can_pass_regime_to_skip_detection(self):
        from models.regime_detector import RegimeType
        ens = self._make_ensemble(0.0, 0.0, 0.0, RegimeType.MEAN_REVERTING)
        out = ens.predict(_make_bars(50), regime=RegimeType.TRENDING)
        assert out["regime"] == RegimeType.TRENDING
        ens._regime_detector.detect.assert_not_called()
