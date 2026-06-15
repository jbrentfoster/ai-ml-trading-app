"""
Unit tests for dashboard/pages/9_Account.py:_enrich_positions.

The function cross-references live IBKR open orders with held positions so
the page doesn't render cancelled bracket legs as if they were still
protecting the position (the SNOW 2026-05-19 stale-TP bug).

yfinance is mocked so tests run offline.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# Page 9 lives outside the standard package tree (dashboard/pages/9_Account.py)
# and its filename starts with a digit, so we load it via importlib.
_PAGE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "pages" / "9_Account.py"


@pytest.fixture(scope="module")
def page_module():
    spec = importlib.util.spec_from_file_location("page_9_account", _PAGE_PATH)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules["page_9_account"] = mod
    # Patch yfinance + streamlit-rendering so the import doesn't try to render
    # the page during test collection.
    with patch("yfinance.Ticker"), patch("streamlit.set_page_config"):
        spec.loader.exec_module(mod)
    return mod


def _mock_yf_history(prices: dict[str, float]):
    """
    Build a yf.Ticker(...).history(period="1d") mock that returns a 1-row
    Close-only DataFrame for each requested symbol.
    """
    def _factory(symbol):
        t = MagicMock()
        if symbol in prices and prices[symbol] is not None:
            t.history.return_value = pd.DataFrame({"Close": [prices[symbol]]})
        else:
            t.history.return_value = pd.DataFrame()
        return t
    return _factory


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEnrichPositions:
    """
    Each test sets up:
      • positions     — IBKR list-of-dicts shape (symbol, quantity, avg_cost)
      • risk_levels   — order_decisions snapshot (entry_price/stop_price/tp_price)
      • open_orders   — IBKRConnection.get_open_orders() shape

    Then asserts the resulting DataFrame's stop_loss / take_profit /
    trail_amount / trail_trigger columns reflect the cross-referenced live
    state, not just whatever `order_decisions` had cached.
    """

    def _risk(self, **kw):
        defaults = {"entry_price": 100.0, "stop_price": 95.0,
                    "take_profit_price": 110.0}
        defaults.update(kw)
        return defaults

    def test_bracket_alive_shows_stop_and_tp(self, page_module):
        """Both legs present in open_orders → both stop and TP carry through."""
        positions = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
        risk_levels = {"AAPL": self._risk()}
        orders = [
            {"symbol": "AAPL", "action": "SELL", "order_type": "LMT",
             "limit_price": 110.0, "stop_price": None, "trail_stop_price": None},
            {"symbol": "AAPL", "action": "SELL", "order_type": "STP",
             "limit_price": None, "stop_price": 95.0, "trail_stop_price": None},
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        assert row["stop_loss"]    == pytest.approx(95.0)
        assert row["take_profit"]  == pytest.approx(110.0)
        assert pd.isna(row["trail_amount"])
        assert pd.isna(row["trail_trigger"])

    def test_trail_active_blanks_tp_and_stop(self, page_module):
        """Once a bracket converts to TRAIL, the original LMT and STP are gone.
        Page should not surface them as if they were still alive (the SNOW
        2026-05-19 stale-card bug)."""
        positions = [{"symbol": "SNOW", "quantity": 100, "avg_cost": 136.50}]
        risk_levels = {"SNOW": self._risk(
            entry_price=136.50, stop_price=123.13, take_profit_price=159.44,
        )}
        orders = [
            {"symbol": "SNOW", "action": "SELL", "order_type": "TRAIL",
             "limit_price": None, "stop_price": 4.0,        # auxPrice = trail dist
             "trail_stop_price": 165.55},                    # live ratcheted trigger
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"SNOW": 169.55})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        # Original bracket prices NOT shown — IBKR no longer holds them.
        assert pd.isna(row["stop_loss"])
        assert pd.isna(row["take_profit"])
        # Trail surfaces.
        assert row["trail_amount"]  == pytest.approx(4.0)
        assert row["trail_trigger"] == pytest.approx(165.55)
        # Trail distance: (current - trigger) / current → (169.55 - 165.55) / 169.55
        assert row["trail_dist_pct"] == pytest.approx(
            (169.55 - 165.55) / 169.55 * 100, rel=1e-6,
        )

    def test_trail_without_trigger_still_surfaces_amount(self, page_module):
        """Until IBKR sends the first ratchet update, trail_stop_price is None.
        The trail distance still comes through; the card branch handles the
        None trigger separately ("trigger pending")."""
        positions = [{"symbol": "SNOW", "quantity": 100, "avg_cost": 136.50}]
        risk_levels = {"SNOW": self._risk()}
        orders = [
            {"symbol": "SNOW", "action": "SELL", "order_type": "TRAIL",
             "limit_price": None, "stop_price": 4.0, "trail_stop_price": None},
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"SNOW": 169.55})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        assert row["trail_amount"]  == pytest.approx(4.0)
        assert pd.isna(row["trail_trigger"])
        assert pd.isna(row["trail_dist_pct"])

    def test_partial_cancel_keeps_remaining_leg(self, page_module):
        """If only the STP got cancelled (e.g. one-sided modification), the
        TP card stays alive and the stop card disappears."""
        positions = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
        risk_levels = {"AAPL": self._risk()}
        orders = [
            {"symbol": "AAPL", "action": "SELL", "order_type": "LMT",
             "limit_price": 110.0, "stop_price": None, "trail_stop_price": None},
            # No STP leg
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        assert pd.isna(row["stop_loss"])              # STP gone
        assert row["take_profit"] == pytest.approx(110.0)  # LMT still alive

    def test_no_orders_for_symbol_blanks_both(self, page_module):
        """Symbol with no matching SELL legs in open_orders → both columns
        blank.  This is the manual-position / orphan-position case."""
        positions = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
        risk_levels = {"AAPL": self._risk()}
        orders: list[dict] = []  # nothing live for any symbol
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        assert pd.isna(row["stop_loss"])
        assert pd.isna(row["take_profit"])
        assert pd.isna(row["trail_amount"])

    def test_orders_none_preserves_legacy_behaviour(self, page_module):
        """Passing open_orders=None means "no information to cross-reference"
        — the function leaves the order_decisions values alone.  Keeps the
        function safe for callers (e.g. tests) that don't supply orders."""
        positions = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
        risk_levels = {"AAPL": self._risk()}
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0})
            df = page_module._enrich_positions(positions, risk_levels, None)

        row = df.iloc[0]
        # Both prices come straight from risk_levels (no cross-ref applied).
        assert row["stop_loss"]   == pytest.approx(95.0)
        assert row["take_profit"] == pytest.approx(110.0)

    def test_buy_orders_ignored(self, page_module):
        """Only SELL legs count as bracket exits.  A still-pending BUY entry
        on the same symbol must not be confused for a TP / stop / trail."""
        positions = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
        risk_levels = {"AAPL": self._risk()}
        orders = [
            {"symbol": "AAPL", "action": "BUY", "order_type": "LMT",
             "limit_price": 99.0, "stop_price": None, "trail_stop_price": None},
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        row = df.iloc[0]
        # BUY LMT must NOT be read as a take-profit.
        assert pd.isna(row["take_profit"])
        assert pd.isna(row["stop_loss"])

    def test_ibkr_price_preferred_over_yfinance(self, page_module):
        """When the caller supplies an IBKR price, it drives current_price /
        market_value / unrealized_pnl — not the yfinance close.  This is the
        2026-06-15 fix: the table must reconcile with the IBKR-sourced headline
        Unrealized P&L, and yfinance lags IBKR's marks outside RTH."""
        positions = [{"symbol": "MU", "quantity": 40, "avg_cost": 994.94}]
        risk_levels = {"MU": self._risk(
            entry_price=994.94, stop_price=950.0, take_profit_price=1100.0,
        )}
        orders: list[dict] = []
        # yfinance would say 981.61 (stale); IBKR says 1062.75.
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"MU": 981.61})
            df = page_module._enrich_positions(
                positions, risk_levels, orders, prices={"MU": 1062.75},
            )

        row = df.iloc[0]
        assert row["current_price"]   == pytest.approx(1062.75)
        assert row["unrealized_pnl"]  == pytest.approx(40 * (1062.75 - 994.94))
        # yfinance must NOT have been consulted for a symbol IBKR priced.
        mock_yf.Ticker.assert_not_called()

    def test_falls_back_to_yfinance_when_ibkr_price_missing(self, page_module):
        """A None IBKR price for a symbol falls back to that symbol's yfinance
        close (get_last_price returning None despite its own tier-3 fallback —
        e.g. a delisted/illiquid name)."""
        positions = [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
            {"symbol": "MSFT", "quantity":  5, "avg_cost": 300.0},
        ]
        risk_levels = {"AAPL": self._risk(), "MSFT": self._risk()}
        orders: list[dict] = []
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0, "MSFT": 305.0})
            df = page_module._enrich_positions(
                positions, risk_levels, orders,
                prices={"AAPL": 111.0, "MSFT": None},  # MSFT must fall back
            )

        aapl = df[df["symbol"] == "AAPL"].iloc[0]
        msft = df[df["symbol"] == "MSFT"].iloc[0]
        assert aapl["current_price"] == pytest.approx(111.0)   # IBKR
        assert msft["current_price"] == pytest.approx(305.0)   # yfinance fallback

    def test_cross_symbol_orders_dont_leak(self, page_module):
        """Open orders for SYMA must not affect SYMB's row."""
        positions = [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
            {"symbol": "MSFT", "quantity":  5, "avg_cost": 300.0},
        ]
        risk_levels = {
            "AAPL": self._risk(),
            "MSFT": self._risk(entry_price=300.0, stop_price=290.0, take_profit_price=320.0),
        }
        orders = [
            # Only AAPL has an active TP; MSFT has nothing live.
            {"symbol": "AAPL", "action": "SELL", "order_type": "LMT",
             "limit_price": 110.0, "stop_price": None, "trail_stop_price": None},
        ]
        with patch.object(page_module, "yf") as mock_yf:
            mock_yf.Ticker.side_effect = _mock_yf_history({"AAPL": 105.0, "MSFT": 305.0})
            df = page_module._enrich_positions(positions, risk_levels, orders)

        aapl = df[df["symbol"] == "AAPL"].iloc[0]
        msft = df[df["symbol"] == "MSFT"].iloc[0]
        assert aapl["take_profit"] == pytest.approx(110.0)
        assert pd.isna(msft["take_profit"])
        assert pd.isna(msft["stop_loss"])
