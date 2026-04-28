"""
Unit tests for data/universe.py.

All tests use in-memory SQLite or mocks — no live network or Alpaca API needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_asset(symbol: str, is_fixture: bool = False,
                market_cap: float = 5e9,
                avg_dollar_volume: float = 10e6) -> dict:
    return {
        "symbol":            symbol,
        "name":              symbol,
        "asset_class":       "etf" if is_fixture else "us_equity",
        "is_fixture":        is_fixture,
        "market_cap":        market_cap,
        "avg_dollar_volume": avg_dollar_volume,
        "xgb_score":         None,
        "active":            True,
        "added_at":          _now(),
        "last_scored_at":    None,
        "removed_at":        None,
    }


def _make_bars(close: float = 100.0, volume: float = 1_000_000, n: int = 20) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open":   close,
        "High":   close * 1.01,
        "Low":    close * 0.99,
        "Close":  close,
        "Volume": volume,
    }, index=idx)


# ── Stage 1 ───────────────────────────────────────────────────────────────────

class TestStage1:

    def test_stage1_returns_fixtures_even_if_alpaca_fails(self):
        """Fixtures are always included even when Alpaca raises."""
        from data.universe import UniverseSelector

        with patch("data.universe.config") as mock_cfg, \
             patch("data.universe.log_universe_run"):
            mock_cfg.universe.permanent_fixtures = ["SPY", "QQQ"]
            mock_cfg.universe.stage1_max = 5000
            mock_cfg.alpaca.api_key    = "key"
            mock_cfg.alpaca.secret_key = "secret"

            sel = UniverseSelector.__new__(UniverseSelector)
            sel._cfg = mock_cfg.universe
            sel._xgb = None

            with patch("alpaca.trading.client.TradingClient",
                       side_effect=RuntimeError("network error")):
                assets = sel._stage1_fetch(run_id="test-run")

        syms = {a["symbol"] for a in assets}
        assert "SPY" in syms
        assert "QQQ" in syms

    def test_stage1_filters_inactive_assets(self):
        """Assets with status != 'active' or tradable=False are excluded."""
        from data.universe import UniverseSelector

        active_asset   = MagicMock(symbol="AAPL", status="active",   tradable=True,  name="Apple")
        inactive_asset = MagicMock(symbol="DEAD", status="inactive", tradable=True,  name="Dead")
        untradable     = MagicMock(symbol="NTRB", status="active",   tradable=False, name="NTR")

        with patch("data.universe.config") as mock_cfg, \
             patch("data.universe.log_universe_run"), \
             patch("alpaca.trading.client.TradingClient") as mock_client_cls:
            mock_cfg.universe.permanent_fixtures = []
            mock_cfg.universe.stage1_max = 5000
            mock_cfg.universe.allowed_exchanges = []
            mock_cfg.alpaca.api_key    = "key"
            mock_cfg.alpaca.secret_key = "secret"

            mock_client = MagicMock()
            mock_client.get_all_assets.return_value = [active_asset, inactive_asset, untradable]
            mock_client_cls.return_value = mock_client

            sel = UniverseSelector.__new__(UniverseSelector)
            sel._cfg = mock_cfg.universe
            sel._xgb = None

            assets = sel._stage1_fetch(run_id="test-run")

        syms = {a["symbol"] for a in assets}
        assert "AAPL" in syms
        assert "DEAD" not in syms
        assert "NTRB" not in syms

    def test_stage1_raises_universe_error_when_no_keys(self):
        """UniverseError raised when API keys are missing."""
        from data.universe import UniverseSelector, UniverseError

        with patch("data.universe.config") as mock_cfg, \
             patch("data.universe.log_universe_run"):
            mock_cfg.universe.permanent_fixtures = []
            mock_cfg.universe.stage1_max = 5000
            mock_cfg.alpaca.api_key    = ""
            mock_cfg.alpaca.secret_key = ""

            sel = UniverseSelector.__new__(UniverseSelector)
            sel._cfg = mock_cfg.universe
            sel._xgb = None

            with pytest.raises(UniverseError, match="ALPACA_API_KEY"):
                sel._stage1_fetch(run_id="test-run")


# ── Stage 2 ───────────────────────────────────────────────────────────────────

class TestStage2:

    def _sel(self, min_mkt_cap=1e9, min_dv=5e6):
        from data.universe import UniverseSelector
        sel = UniverseSelector.__new__(UniverseSelector)
        sel._xgb = None
        sel._cfg = MagicMock()
        sel._cfg.min_market_cap        = min_mkt_cap
        sel._cfg.min_avg_dollar_volume = min_dv
        sel._cfg.stage2_max            = 300
        sel._cfg.stage3_max            = 50
        return sel

    def _mock_ticker(self, bars: pd.DataFrame):
        """Return a mock yfinance.Ticker whose .history() returns `bars`."""
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = bars
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        return mock_yf

    def test_stage2_filters_low_market_cap(self):
        """Symbol passing dollar-volume but failing market-cap is dropped."""
        assets = [_make_asset("SMALL", market_cap=5e8)]
        # $50 × 2M shares = $100M/day — passes DV check
        bars   = _make_bars(close=50.0, volume=2_000_000)
        mock_yf = self._mock_ticker(bars)

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.yf", mock_yf), \
             patch("data.universe.FundamentalsClient") as mock_fc:
            mock_fc.return_value.get.return_value = {"market_cap": 5e8}
            result = self._sel(min_mkt_cap=1e9)._stage2_filter(assets, "r1")

        assert len(result) == 0

    def test_stage2_fixtures_bypass_filter(self):
        """Fixtures with low market cap still pass Stage 2."""
        assets  = [_make_asset("SPY", is_fixture=True, market_cap=100e9)]
        mock_yf = self._mock_ticker(_make_bars())

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.yf", mock_yf), \
             patch("data.universe.FundamentalsClient") as mock_fc:
            mock_fc.return_value.get.return_value = {"market_cap": 1e6}
            result = self._sel()._stage2_filter(assets, "r1")

        assert len(result) == 1
        assert result[0]["symbol"] == "SPY"

    def test_stage2_filters_low_dollar_volume(self):
        """Symbol failing dollar-volume check is dropped (Pass 1 rejects it)."""
        assets = [_make_asset("ILLIQ", market_cap=10e9)]
        # $2 × 100k shares = $200k/day (< $5M threshold)
        bars    = _make_bars(close=2.0, volume=100_000)
        mock_yf = self._mock_ticker(bars)

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.yf", mock_yf), \
             patch("data.universe.FundamentalsClient") as mock_fc:
            mock_fc.return_value.get.return_value = {"market_cap": 10e9}
            result = self._sel(min_dv=5e6)._stage2_filter(assets, "r1")

        assert len(result) == 0

    def test_stage2_passes_adequate_dollar_volume(self):
        """$50 × 200k shares = $10M/day passes $5M threshold."""
        assets  = [_make_asset("LIQD", market_cap=5e9)]
        bars    = _make_bars(close=50.0, volume=200_000)
        mock_yf = self._mock_ticker(bars)

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.yf", mock_yf), \
             patch("data.universe.FundamentalsClient") as mock_fc:
            mock_fc.return_value.get.return_value = {"market_cap": 5e9}
            result = self._sel(min_dv=5e6)._stage2_filter(assets, "r1")

        assert len(result) == 1
        assert result[0]["symbol"] == "LIQD"


# ── Stage 3 ───────────────────────────────────────────────────────────────────

class TestStage3:

    def _sel(self, xgb_model=None, stage3_max=3):
        from data.universe import UniverseSelector
        sel = UniverseSelector.__new__(UniverseSelector)
        sel._xgb = xgb_model
        sel._cfg = MagicMock()
        sel._cfg.stage3_max = stage3_max
        sel._cfg.permanent_fixtures = []
        return sel

    def test_stage3_no_xgb_sorts_by_market_cap(self):
        """When xgb_model is None, top-N by market_cap are selected."""
        assets = [
            _make_asset("BIG",   market_cap=100e9),
            _make_asset("MED",   market_cap=50e9),
            _make_asset("SMALL", market_cap=5e9),
            _make_asset("TINY",  market_cap=1e9),
        ]
        with patch("data.universe.log_universe_run"):
            result = self._sel(stage3_max=2)._stage3_score(assets, "r1")
        syms = [a["symbol"] for a in result]
        assert syms == ["BIG", "MED"]

    def test_stage3_with_xgb_uses_scores(self):
        """XGBoost scores determine ranking when model is provided."""
        assets = [
            _make_asset("LOW_CAP_HIGH_SCORE", market_cap=1e9),
            _make_asset("HIGH_CAP_LOW_SCORE", market_cap=100e9),
        ]
        mock_xgb = MagicMock()
        # Low-cap symbol gets higher XGB score
        mock_xgb.predict.side_effect = lambda df: (
            0.9 if df is not None else 0.0
        )

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.IndicatorEngine") as mock_ie:

            mock_ie.return_value.run.return_value = pd.DataFrame({"Close": [100]})
            # Patch predict to return different scores per call
            scores = iter([0.9, 0.1])
            mock_xgb.predict.side_effect = lambda df: next(scores)
            result = self._sel(xgb_model=mock_xgb, stage3_max=1)._stage3_score(assets, "r1")

        assert result[0]["symbol"] == "LOW_CAP_HIGH_SCORE"

    def test_stage3_fixtures_always_included(self):
        """Fixtures are retained even if they would be below the top-N cutoff."""
        fixture = _make_asset("SPY", is_fixture=True, market_cap=500e9)
        others  = [_make_asset(f"SYM{i}", market_cap=float(10 - i) * 1e9) for i in range(5)]

        with patch("data.universe.log_universe_run"):
            result = self._sel(stage3_max=2)._stage3_score([fixture] + others, "r1")

        syms = [a["symbol"] for a in result]
        assert "SPY" in syms

    def test_stage3_handles_xgb_exception(self):
        """XGB exception per symbol is caught; score defaults to 0.0."""
        assets   = [_make_asset("ERRSY", market_cap=5e9)]
        mock_xgb = MagicMock()
        mock_xgb.predict.side_effect = RuntimeError("model error")

        with patch("data.universe.log_universe_run"), \
             patch("data.universe.IndicatorEngine") as mock_ie:

            mock_ie.return_value.run.return_value = pd.DataFrame({"Close": [100]})
            result = self._sel(xgb_model=mock_xgb, stage3_max=5)._stage3_score(assets, "r1")

        assert result[0]["xgb_score"] == 0.0


# ── Run result / persistence ──────────────────────────────────────────────────

class TestRunResult:

    def test_run_full_persists_to_db(self):
        """run_full() calls upsert_universe_asset for each selected symbol."""
        from data.universe import UniverseSelector

        assets = [_make_asset("AAPL"), _make_asset("SPY", is_fixture=True)]

        with patch.object(UniverseSelector, "_stage1_fetch", return_value=assets), \
             patch.object(UniverseSelector, "_stage2_filter", return_value=assets), \
             patch.object(UniverseSelector, "_stage3_score", return_value=assets), \
             patch.object(UniverseSelector, "_persist_active") as mock_persist, \
             patch("data.universe.log_universe_run"):

            sel    = UniverseSelector()
            result = sel.run_full()

        mock_persist.assert_called_once()
        assert result.stage1_count == 2
        assert result.stage3_count == 2

    def test_run_rescore_skips_stage1_and_stage2(self):
        """run_rescore() does not call _stage1_fetch."""
        from data.universe import UniverseSelector

        assets = [_make_asset("MSFT")]

        with patch("data.universe.get_universe_assets",
                   return_value=pd.DataFrame([assets[0]])), \
             patch.object(UniverseSelector, "_stage1_fetch") as mock_s1, \
             patch.object(UniverseSelector, "_stage3_score", return_value=assets), \
             patch.object(UniverseSelector, "_persist_active"), \
             patch("data.universe.log_universe_run"):

            sel = UniverseSelector()
            sel.run_rescore()

        mock_s1.assert_not_called()

    def test_universe_run_result_counts(self):
        """UniverseRunResult dataclass stores stage counts correctly."""
        from data.universe import UniverseRunResult
        r = UniverseRunResult(
            run_id="abc",
            run_type="full",
            stage1_count=1000,
            stage2_count=200,
            stage3_count=50,
        )
        assert r.stage1_count == 1000
        assert r.stage2_count == 200
        assert r.stage3_count == 50
        assert r.run_type == "full"

    def test_get_watchlist_returns_active_only(self):
        """get_watchlist() returns active symbol list from DB."""
        from data.universe import UniverseSelector

        df = pd.DataFrame([{"symbol": "AAPL"}, {"symbol": "SPY"}])
        with patch("data.universe.get_universe_assets", return_value=df):
            sel    = UniverseSelector()
            result = sel.get_watchlist()

        assert result == ["AAPL", "SPY"]


# ── DB helpers (in-memory SQLite) ─────────────────────────────────────────────

class TestDbHelpers:

    @pytest.fixture
    def db_engine(self, tmp_path, monkeypatch):
        """In-memory SQLite engine with the full schema."""
        from sqlalchemy import create_engine
        from data.database import Base, _migrate

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)
        _migrate(engine)

        import data.database as db_module
        monkeypatch.setattr(db_module, "_engine", engine)
        return engine

    def test_upsert_universe_asset_roundtrip(self, db_engine):
        from data.database import upsert_universe_asset, get_universe_assets

        asset = {
            "symbol":            "TEST",
            "name":              "Test Corp",
            "asset_class":       "us_equity",
            "is_fixture":        False,
            "stage":             3,
            "market_cap":        5e9,
            "avg_dollar_volume": 10e6,
            "xgb_score":         0.42,
            "active":            True,
            "added_at":          _now(),
            "last_scored_at":    _now(),
            "removed_at":        None,
        }
        upsert_universe_asset(asset)
        df = get_universe_assets(active_only=True)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "TEST"
        assert abs(df.iloc[0]["xgb_score"] - 0.42) < 1e-6

    def test_log_universe_run_roundtrip(self, db_engine):
        from data.database import log_universe_run, get_universe_run_log

        record = {
            "run_id":           str(uuid.uuid4()),
            "run_type":         "full",
            "stage":            1,
            "symbol_count":     500,
            "duration_seconds": 3.14,
            "recorded_at":      _now(),
            "notes":            None,
        }
        log_universe_run(record)
        df = get_universe_run_log(limit=10)
        assert len(df) == 1
        assert df.iloc[0]["run_type"] == "full"
        assert df.iloc[0]["symbol_count"] == 500
