"""
Step 3 Verification Script
==========================
End-to-end validation of the ML signal generation layer:
  1. Config — AlpacaConfig and MLConfig fields present
  2. Database — new tables created successfully
  3. Fundamentals — FundamentalsClient returns a feature vector
  4. Models instantiate without errors
  5. DatasetBuilder — fit/transform/build with synthetic data
  6. XGBoostModel — trains and predicts on synthetic data
  7. RegimeDetector — classifies synthetic bars
  8. SignalGate — passes / rejects signals correctly
  9. EnsembleModel — predict() returns correct shape
 10. Walk-forward — MLWalkForwardOrchestrator runs on small dataset

No IBKR connection or GPU required.
Alpaca and FinBERT API calls are stubbed so the script works offline.

Run with:
    python verify_signals.py
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows terminals default to cp1252; force UTF-8 so output works.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "OK"
FAIL = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []


def check(status: str, label: str, detail: str = "") -> None:
    results.append((status, label, detail))
    icon = {"OK": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}.get(status, "[?]")
    line = f"{icon} {label}"
    if detail:
        line += f": {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {title}")
    print(f"{'-' * 60}")


def _make_bars(n: int = 300, start: str = "2022-01-03") -> pd.DataFrame:
    rng = np.random.default_rng(0)
    dates  = pd.date_range(start, periods=n, freq="B")
    closes = 100 + rng.normal(0, 1, n).cumsum()
    return pd.DataFrame(
        {
            "Open":   closes * rng.uniform(0.99, 1.0, n),
            "High":   closes * rng.uniform(1.00, 1.01, n),
            "Low":    closes * rng.uniform(0.99, 1.00, n),
            "Close":  closes,
            "Volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
            "rsi_14":       rng.uniform(30, 70, n),
            "macd":         rng.normal(0, 0.5, n),
            "macd_signal":  rng.normal(0, 0.4, n),
            "macd_hist":    rng.normal(0, 0.2, n),
            "bb_upper":     closes + 2,
            "bb_middle":    closes,
            "bb_lower":     closes - 2,
            "ema_9":        closes * 0.99,
            "ema_21":       closes * 0.98,
            "ema_50":       closes * 0.97,
            "atr_14":       rng.uniform(0.5, 2.0, n),
            "volume_sma_20": rng.uniform(1e6, 4e6, n),
        },
        index=dates,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 60)
    print("  AI Trading App - Step 3 Signal Generation Verification")
    print("=" * 60)

    # ── 1. Config ─────────────────────────────────────────────────────────────
    section("Check 1 — Config (AlpacaConfig + MLConfig)")
    try:
        from config.settings import config
        _ = config.alpaca.api_key
        _ = config.alpaca.news_lookback_days
        _ = config.ml.lstm_hidden_size
        _ = config.ml.ensemble_lstm_weight
        _ = config.ml.signal_threshold
        total = (
            config.ml.ensemble_lstm_weight
            + config.ml.ensemble_xgb_weight
            + config.ml.ensemble_finbert_weight
        )
        if abs(total - 1.0) < 1e-6:
            check(PASS, "AlpacaConfig and MLConfig present; ensemble weights sum to 1.0")
        else:
            check(FAIL, "Ensemble weights do not sum to 1.0", f"sum={total:.4f}")
    except Exception as exc:
        check(FAIL, "Config import failed", str(exc))

    # ── 2. Database ───────────────────────────────────────────────────────────
    section("Check 2 — Database new tables")
    try:
        from data.database import (
            get_engine, FundamentalData, NewsCache,
            SignalLog, EnsembleWeightHistory, WalkForwardResult,
        )
        engine = get_engine()
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(engine)
        tables = inspector.get_table_names()
        expected = [
            "ohlcv_bars", "indicator_snapshots", "fundamental_data",
            "news_cache", "signal_log", "ensemble_weight_history",
            "walk_forward_results",
        ]
        missing = [t for t in expected if t not in tables]
        if missing:
            check(FAIL, "Missing tables", str(missing))
        else:
            check(PASS, f"All {len(expected)} tables present in database")
    except Exception as exc:
        check(FAIL, "Database table check failed", str(exc))

    # ── 3. Fundamentals ───────────────────────────────────────────────────────
    section("Check 3 — FundamentalsClient")
    try:
        from data.fundamentals import FundamentalsClient
        mock_info = {
            "marketCap": 3e12, "trailingPE": 28.0, "forwardPE": 25.0,
            "priceToBook": 45.0, "returnOnEquity": 1.5,
        }
        with patch("data.fundamentals.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            client = FundamentalsClient()
            vec = client.get_feature_vector("AAPL")
        assert "pe_ratio" in vec
        check(PASS, f"FundamentalsClient returns {len(vec)}-feature vector")
    except Exception as exc:
        check(FAIL, "FundamentalsClient failed", str(exc))

    # ── 4. Model imports ──────────────────────────────────────────────────────
    section("Check 4 — Model class imports")
    for cls_path in [
        "models.base_model.BaseModel",
        "models.lstm_model.LSTMModel",
        "models.xgboost_model.XGBoostModel",
        "models.finbert_model.FinBERTModel",
        "models.regime_detector.RegimeDetector",
        "models.ensemble.EnsembleModel",
        "models.signal_gate.SignalGate",
        "models.walk_forward.MLWalkForwardOrchestrator",
    ]:
        try:
            module, cls = cls_path.rsplit(".", 1)
            import importlib
            mod = importlib.import_module(module)
            klass = getattr(mod, cls)
            check(PASS, f"{cls_path} importable")
        except Exception as exc:
            check(FAIL, f"{cls_path} import failed", str(exc))

    # ── 5. DatasetBuilder ─────────────────────────────────────────────────────
    section("Check 5 — DatasetBuilder fit/transform/build")
    try:
        from models.lstm_model import DatasetBuilder, _FEATURE_COLS
        df = _make_bars(200)
        builder = DatasetBuilder(seq_len=20, feature_cols=_FEATURE_COLS)
        builder.fit(df)
        arr = builder.transform(df)
        X, y = builder.build(df, forward_bars=5)
        assert arr.shape == (len(df), len(_FEATURE_COLS)), "transform shape mismatch"
        assert X.shape[1] == 20, "sequence length mismatch"
        assert len(X) == len(y), "X/y length mismatch"
        check(PASS, f"DatasetBuilder: X={X.shape}, y={y.shape}, "
              f"features={arr.shape[1]}")
    except Exception as exc:
        check(FAIL, "DatasetBuilder failed", str(exc))

    # ── 6. XGBoostModel ───────────────────────────────────────────────────────
    section("Check 6 — XGBoostModel train + predict")
    try:
        from models.xgboost_model import XGBoostModel
        df = _make_bars(250)
        with patch("models.xgboost_model.FundamentalsClient") as MockFund:
            MockFund.return_value.get_feature_vector.return_value = {
                "pe_ratio": 25.0, "market_cap": 3e12, "forward_pe": 22.0,
            }
            model = XGBoostModel(symbol="AAPL")
            model.train(df)
            score = model.predict(df)
        assert -1.0 <= score <= 1.0, f"score {score} out of range"
        check(PASS, f"XGBoostModel trained and predicted (score={score:.3f})")
    except Exception as exc:
        check(FAIL, "XGBoostModel failed", str(exc))

    # ── 7. RegimeDetector ─────────────────────────────────────────────────────
    section("Check 7 — RegimeDetector")
    try:
        from models.regime_detector import RegimeDetector, RegimeType
        df = _make_bars(100)

        with patch.object(RegimeDetector, "_get_vix", return_value=30.0):
            det    = RegimeDetector()
            regime = det.detect(df)
        assert regime == RegimeType.HIGH_VOLATILITY

        with patch.object(RegimeDetector, "_get_vix", return_value=12.0):
            det    = RegimeDetector()
            regime = det.detect(df)
        assert regime in (RegimeType.TRENDING, RegimeType.MEAN_REVERTING)

        check(PASS, "RegimeDetector classifies HIGH_VOLATILITY and non-volatile correctly")
    except Exception as exc:
        check(FAIL, "RegimeDetector failed", str(exc))

    # ── 8. SignalGate ─────────────────────────────────────────────────────────
    section("Check 8 — SignalGate filter logic")
    try:
        from models.signal_gate import SignalGate
        from models.regime_detector import RegimeDetector, RegimeType
        df = _make_bars(80)

        with patch.object(RegimeDetector, "detect", return_value=RegimeType.MEAN_REVERTING):
            gate = SignalGate()

            # Should HOLD — score below threshold
            r1 = gate.evaluate("AAPL", df, {"lstm": 0.1, "xgb": 0.1, "finbert": 0.05, "ensemble": 0.10})
            assert r1.signal == "HOLD", f"Expected HOLD, got {r1.signal}"

            # Should BUY — all aligned, score above threshold
            r2 = gate.evaluate("AAPL", df, {"lstm": 0.8, "xgb": 0.7, "finbert": 0.6, "ensemble": 0.70})
            assert r2.signal == "BUY", f"Expected BUY, got {r2.signal}"

        check(PASS, "SignalGate: HOLD below threshold, BUY when aligned above threshold")
    except Exception as exc:
        check(FAIL, "SignalGate failed", str(exc))

    # ── 9. EnsembleModel.predict() ────────────────────────────────────────────
    section("Check 9 — EnsembleModel predict structure")
    try:
        from models.ensemble import EnsembleModel

        mock_score = 0.45
        with (
            patch("models.ensemble.LSTMModel") as MockLSTM,
            patch("models.ensemble.XGBoostModel") as MockXGB,
            patch("models.ensemble.FinBERTModel") as MockFB,
            patch("models.ensemble.log_ensemble_weights"),
        ):
            MockLSTM.return_value.predict.return_value = mock_score
            MockXGB.return_value.predict.return_value  = mock_score
            MockFB.return_value.predict.return_value   = mock_score

            ens    = EnsembleModel(symbol="AAPL")
            df     = _make_bars(100)
            scores = ens.predict(df)

        assert "lstm" in scores and "xgb" in scores and "finbert" in scores and "ensemble" in scores
        assert -1.0 <= scores["ensemble"] <= 1.0
        check(PASS, f"EnsembleModel.predict() returns correct keys (ensemble={scores['ensemble']:.3f})")
    except Exception as exc:
        check(FAIL, "EnsembleModel failed", str(exc))

    # ── 10. Walk-forward orchestrator ─────────────────────────────────────────
    section("Check 10 — MLWalkForwardOrchestrator (2-fold mini-run)")
    try:
        from models.walk_forward import MLWalkForwardOrchestrator
        from models.ensemble import EnsembleModel

        df = _make_bars(800)

        def _fake_train(self, df):
            pass

        def _fake_predict(self, df):
            return {"lstm": 0.5, "xgb": 0.4, "finbert": 0.3, "ensemble": 0.4}

        def _fake_evaluate(self, df):
            return {"lstm": {"sharpe_ratio": 0.5, "total_return": 0.02},
                    "xgb":  {"sharpe_ratio": 0.6, "total_return": 0.03},
                    "finbert": {"sharpe_ratio": 0.3, "total_return": 0.01}}

        def _fake_rebalance(self, metrics):
            pass

        with (
            patch.object(EnsembleModel, "train",    _fake_train),
            patch.object(EnsembleModel, "predict",  _fake_predict),
            patch.object(EnsembleModel, "evaluate", _fake_evaluate),
            patch.object(EnsembleModel, "rebalance", _fake_rebalance),
            patch("models.walk_forward.log_walk_forward_result"),
        ):
            from config.settings import config
            config.ml.wf_n_splits   = 2
            config.ml.wf_train_bars = 252
            config.ml.wf_test_bars  = 63

            orch    = MLWalkForwardOrchestrator(symbol="AAPL")
            results_wf = orch.run(df)

        n_folds = len(results_wf)
        if n_folds >= 1:
            check(PASS, f"MLWalkForwardOrchestrator completed {n_folds} fold(s)")
        else:
            check(FAIL, "No folds produced")
    except Exception as exc:
        check(FAIL, "MLWalkForwardOrchestrator failed", str(exc))

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    passed = sum(1 for s, _, _ in results if s == PASS)
    warned = sum(1 for s, _, _ in results if s == WARN)
    failed = sum(1 for s, _, _ in results if s == FAIL)
    total  = len(results)
    print(f"  Results: {passed} passed / {warned} warned / {failed} failed  ({total} checks)")
    print("=" * 60)

    if failed:
        print("\n[FAIL] Some checks failed - review output above.")
        sys.exit(1)
    elif warned:
        print("\n[WARN] All checks passed with warnings.")
    else:
        print("\n[PASS] All checks passed - ready for Step 4 (execution engine)")


if __name__ == "__main__":
    main()
