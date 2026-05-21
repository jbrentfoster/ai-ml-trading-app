"""
Unit tests for risk/trailing_stop.py (Phase 3.5).

Uses AsyncMock for the IBKR async methods and a real asyncio event loop so
`run_until_complete(...)` drives the AsyncMocks correctly.  No live IBKR
connection or yfinance calls required.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from risk.trailing_stop import TrailingStopAction, TrailingStopManager


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _no_db_writes(monkeypatch):
    """Prevent TrailingStopManager._persist from hitting the real SQLite DB.

    Individual tests that care about persistence patch this explicitly.
    """
    import data.database as db
    monkeypatch.setattr(db, "log_trailing_stop_action", lambda *_a, **_kw: None)


def _ibkr_stub(positions, open_orders) -> MagicMock:
    """Build a MagicMock IBKR connection with AsyncMock methods."""
    ibkr = MagicMock()
    ibkr.get_positions     = AsyncMock(return_value=positions)
    ibkr.get_open_orders   = AsyncMock(return_value=open_orders)
    ibkr.cancel_order      = AsyncMock(return_value=True)
    ibkr.place_trailing_stop = AsyncMock(return_value=MagicMock())
    return ibkr


def _bracket_orders(symbol: str) -> list[dict]:
    """A minimal bracket: LMT take-profit + STP stop-loss."""
    return [
        {"order_id": 1001, "symbol": symbol, "action": "SELL",
         "order_type": "LMT", "limit_price": 110.0, "stop_price": None,
         "quantity": 100, "status": "Submitted"},
        {"order_id": 1002, "symbol": symbol, "action": "SELL",
         "order_type": "STP", "limit_price": None, "stop_price": 96.0,
         "quantity": 100, "status": "Submitted"},
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTrailingStopManager:

    def test_disabled_returns_empty(self, loop):
        """When trailing_stop_enabled=False, manage() returns [] with no calls."""
        ibkr = _ibkr_stub(positions=[], open_orders=[])
        mgr  = TrailingStopManager(ibkr, loop)
        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = False
            actions = mgr.manage()
        assert actions == []
        ibkr.get_positions.assert_not_called()

    def test_skips_when_trail_already_active(self, loop):
        """Idempotency — a symbol with an existing TRAIL order is left alone."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[
                {"order_id": 2001, "symbol": "AAPL", "action": "SELL",
                 "order_type": "TRAIL", "limit_price": None, "stop_price": 4.0,
                 "quantity": 100, "status": "Submitted"},
            ],
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [108.5]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].action == "SKIPPED"
        assert "already active" in actions[0].reason
        # Entry and current price should be populated from data we already have
        # (avg_cost + latest close).  atr / trail_amount stay None — the trail
        # was sized in a prior run; today's ATR isn't what's protecting it.
        assert actions[0].entry_price   == pytest.approx(100.0)
        assert actions[0].current_price == pytest.approx(108.5)
        assert actions[0].atr is None
        assert actions[0].trail_amount is None
        ibkr.cancel_order.assert_not_called()
        ibkr.place_trailing_stop.assert_not_called()

    def test_trail_already_active_persists_null_for_unmeasured_fields(
        self, loop, monkeypatch
    ):
        """Regression: the "already active" branch used to write 0.0 to atr /
        trail_amount in trailing_stop_log, silently skewing any dashboard
        aggregation. Must persist as None so the DB stores NULL."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "SNOW", "quantity": 200, "avg_cost": 175.50}],
            open_orders=[
                {"order_id": 3001, "symbol": "SNOW", "action": "SELL",
                 "order_type": "TRAIL", "limit_price": None, "stop_price": 8.0,
                 "quantity": 200, "status": "Submitted"},
            ],
        )
        mgr = TrailingStopManager(ibkr, loop)

        captured: list[dict] = []
        import data.database as db
        monkeypatch.setattr(db, "log_trailing_stop_action",
                            lambda rec: captured.append(rec))

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [192.75]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 2.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            mgr.manage(run_id="snow-trail-test")

        assert len(captured) == 1
        rec = captured[0]
        assert rec["action"]        == "SKIPPED"
        assert rec["entry_price"]   == pytest.approx(175.50)
        assert rec["current_price"] == pytest.approx(192.75)
        assert rec["atr"]          is None     # NULL in SQLite
        assert rec["trail_amount"] is None     # NULL in SQLite

    def test_skips_below_activation_threshold(self, loop):
        """Price has not moved +1 ATR yet → no conversion."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [101.5]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0  # need >= 100 + 2 = 102
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].action == "SKIPPED"
        assert "below activation" in actions[0].reason
        ibkr.cancel_order.assert_not_called()
        ibkr.place_trailing_stop.assert_not_called()

    def test_converts_when_above_activation_threshold(self, loop):
        """Price has cleared entry + 1×ATR → cancel TP + STP, submit TRAIL."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [103.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].action == "CONVERTED"
        assert actions[0].shares == 100
        assert actions[0].entry_price == pytest.approx(100.0)
        assert actions[0].current_price == pytest.approx(103.0)
        assert actions[0].atr == pytest.approx(2.0)
        assert actions[0].trail_amount == pytest.approx(4.0)  # 2 × ATR

        # Cancel TP first, then STP — then submit TRAIL.
        assert ibkr.cancel_order.await_count == 2
        assert ibkr.cancel_order.await_args_list[0].args == (1001,)  # TP
        assert ibkr.cancel_order.await_args_list[1].args == (1002,)  # STP

        ibkr.place_trailing_stop.assert_awaited_once()
        kwargs = ibkr.place_trailing_stop.await_args.kwargs
        assert kwargs["symbol"]       == "AAPL"
        assert kwargs["action"]       == "SELL"
        assert kwargs["quantity"]     == 100
        assert kwargs["trail_amount"] == pytest.approx(4.0)

    def test_skips_when_atr_missing(self, loop):
        """No ATR → cannot compute trail distance → skip with reason."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": None}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [103.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert actions[0].action == "SKIPPED"
        assert "no atr" in actions[0].reason.lower()
        ibkr.cancel_order.assert_not_called()

    def test_skips_manual_position_without_bracket(self, loop):
        """Position exists but no TP/STP legs in open orders → manual position, skip."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[],   # no bracket legs
        )
        mgr = TrailingStopManager(ibkr, loop)

        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert actions[0].action == "SKIPPED"
        assert "bracket" in actions[0].reason.lower()

    def test_short_positions_skipped(self, loop):
        """Short positions are out of scope for the long-only trailing stop;
        they're logged as SKIPPED so an unexpected short surfaces in the
        daily log instead of vanishing silently."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": -100, "avg_cost": 100.0}],
            open_orders=[],
        )
        mgr = TrailingStopManager(ibkr, loop)

        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].symbol == "AAPL"
        assert actions[0].action == "SKIPPED"
        assert actions[0].shares == -100
        assert "non-long" in actions[0].reason.lower()
        ibkr.cancel_order.assert_not_called()

    def test_run_id_propagates_to_action_and_persist(self, loop, monkeypatch):
        """run_id passed to manage() must land on the action AND the persisted row."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        captured: list[dict] = []
        import data.database as db
        monkeypatch.setattr(db, "log_trailing_stop_action",
                            lambda rec: captured.append(rec))

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [103.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage(run_id="abc-123")

        assert actions[0].run_id == "abc-123"
        assert len(captured) == 1
        assert captured[0]["run_id"] == "abc-123"
        assert captured[0]["symbol"] == "AAPL"
        assert captured[0]["action"] == "CONVERTED"
        assert captured[0]["trail_amount"] == pytest.approx(4.0)
        assert "decided_at" in captured[0]

    def test_skipped_actions_are_also_persisted(self, loop, monkeypatch):
        """SKIPPED actions must reach the log so users can see what was evaluated."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        captured: list[dict] = []
        import data.database as db
        monkeypatch.setattr(db, "log_trailing_stop_action",
                            lambda rec: captured.append(rec))

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [101.5]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0  # threshold = 102
            cfg.risk.trailing_stop_trail_atr = 2.0
            mgr.manage(run_id="skip-test")

        assert len(captured) == 1
        assert captured[0]["action"] == "SKIPPED"
        assert captured[0]["run_id"] == "skip-test"

    def test_failed_trail_submit_leaves_position_unprotected(self, loop):
        """If TRAIL submission fails after both cancels, action='FAILED'."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        ibkr.place_trailing_stop = AsyncMock(side_effect=RuntimeError("IBKR timeout"))
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars", return_value=pd.DataFrame({"Close": [103.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert actions[0].action == "FAILED"
        assert "place TRAIL failed" in actions[0].reason
        # Both cancels were attempted before the failed submit.
        assert ibkr.cancel_order.await_count == 2


# ── Phase 1: price_source + ratchet detection (intraday-runner plumbing) ──────

class TestPriceSourceParameter:
    """Backward-compat + new override path for the intraday runner.

    The daily signal_runner calls ``manage()`` with no kwargs and must keep
    reading from ohlcv_bars (the legacy path).  The intraday runner passes a
    ``price_source`` callable so mid-day evaluation uses the live IBKR quote
    instead of yesterday's stored close.
    """

    def test_trailing_manager_default_uses_db(self, loop):
        """Backward compat: manage() with no price_source still reads get_bars."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [103.0]})) as mock_get_bars,
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        # The DB read happened — that's the regression guard against
        # accidentally breaking the daily-runner code path while wiring up
        # the intraday-runner one.
        assert mock_get_bars.called, "get_bars should be invoked when price_source is None"
        assert actions[0].action == "CONVERTED"
        assert actions[0].current_price == pytest.approx(103.0)

    def test_trailing_manager_with_price_source_skips_db(self, loop):
        """price_source supplied → get_bars NOT called for current price.

        The activation path uses the live source ($105) instead of whatever
        the DB might contain.  get_latest_indicators is still called (ATR is
        daily-bar-derived and doesn't change intraday — documented).
        """
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)
        live_quote = MagicMock(return_value=105.0)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
            # If get_bars is called by mistake, return a wildly different price
            # so the activation check would fail and the test would catch it.
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [90.0]})) as mock_get_bars,
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            cfg.risk.intraday_trail_conversion_enabled = True
            cfg.risk.intraday_conversion_buffer_atr = 0.5
            actions = mgr.manage(price_source=live_quote, intraday=True)

        live_quote.assert_called_once_with("AAPL")
        assert not mock_get_bars.called, "get_bars must not be called when price_source supplied"
        # Activation: entry $100 + (1.0 + 0.5) × $2 ATR = $103.  Live $105 clears.
        assert actions[0].action == "CONVERTED"
        assert actions[0].current_price == pytest.approx(105.0)


class TestRatchetDetection:
    """The intraday runner's second job: log when IBKR has ratcheted the
    trailing stop up since the last logged entry, so Page 8 can show the
    ratchet history without a separate IBKR poll."""

    def test_ratchet_detected_when_live_trigger_above_prior(self, loop, monkeypatch):
        """Prior log row: current_price=$100, trail_amount=$4 → prior trigger=$96.
        Live IBKR Order.trailStopPrice=$99 → ratchet of +$3 → RATCHETED row."""
        live_trail = {
            "order_id": 5001, "symbol": "AAPL", "action": "SELL",
            "order_type": "TRAIL", "limit_price": None,
            "stop_price": 4.0,                # auxPrice = trail distance
            "trail_stop_price": 99.0,         # IBKR's live ratcheting trigger
            "quantity": 100, "status": "Submitted",
        }
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[live_trail],
        )
        mgr = TrailingStopManager(ibkr, loop)

        # Prior log row: trigger $96 implied by current_price - trail_amount.
        import data.database as db
        monkeypatch.setattr(
            db, "get_latest_trailing_stop_log_for_symbol",
            lambda sym: {
                "current_price": 100.0,
                "trail_amount":  4.0,
                "action":        "CONVERTED",
            },
        )

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [107.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 2.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].action == "RATCHETED"
        assert actions[0].trail_amount == pytest.approx(4.0)
        assert "96.00" in actions[0].reason
        assert "99.00" in actions[0].reason

    def test_no_ratchet_when_live_trigger_unchanged(self, loop, monkeypatch):
        """Live trigger equals prior trigger → SKIPPED, not RATCHETED.
        Defends against floating-point noise emitting spurious ratchet rows."""
        live_trail = {
            "order_id": 5002, "symbol": "AAPL", "action": "SELL",
            "order_type": "TRAIL", "limit_price": None,
            "stop_price": 4.0, "trail_stop_price": 96.005,  # within epsilon
            "quantity": 100, "status": "Submitted",
        }
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[live_trail],
        )
        mgr = TrailingStopManager(ibkr, loop)

        import data.database as db
        monkeypatch.setattr(
            db, "get_latest_trailing_stop_log_for_symbol",
            lambda sym: {"current_price": 100.0, "trail_amount": 4.0,
                         "action": "CONVERTED"},
        )

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [107.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 2.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert actions[0].action == "SKIPPED"
        assert "already active" in actions[0].reason

    def test_no_ratchet_when_no_prior_log_row(self, loop, monkeypatch):
        """First time we see this TRAIL — no prior to compare against."""
        live_trail = {
            "order_id": 5003, "symbol": "AAPL", "action": "SELL",
            "order_type": "TRAIL", "limit_price": None,
            "stop_price": 4.0, "trail_stop_price": 95.0,
            "quantity": 100, "status": "Submitted",
        }
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[live_trail],
        )
        mgr = TrailingStopManager(ibkr, loop)

        import data.database as db
        monkeypatch.setattr(
            db, "get_latest_trailing_stop_log_for_symbol", lambda sym: None,
        )

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [107.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 2.0
            actions = mgr.manage()

        assert actions[0].action == "SKIPPED"

    def test_no_ratchet_when_live_trigger_not_yet_reported(self, loop):
        """IBKR hasn't sent the first trailStopPrice update yet → SKIPPED.
        Common on the first intraday check after a morning conversion."""
        live_trail = {
            "order_id": 5004, "symbol": "AAPL", "action": "SELL",
            "order_type": "TRAIL", "limit_price": None,
            "stop_price": 4.0,
            "trail_stop_price": None,         # not yet populated by IBKR
            "quantity": 100, "status": "Submitted",
        }
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=[live_trail],
        )
        mgr = TrailingStopManager(ibkr, loop)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_bars",
                  return_value=pd.DataFrame({"Close": [107.0]})),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 2.0
            actions = mgr.manage()

        # No live trigger to compare → can't claim a ratchet.  SKIPPED, not
        # RATCHETED.  No DB lookup needed either (short-circuits before that).
        assert actions[0].action == "SKIPPED"


class TestIntradayConversionGate:
    """When intraday=True AND config.intraday_trail_conversion_enabled=False
    (the default), conversions are suppressed even if the position would
    otherwise qualify.  Buffer applied on top of activation_atr when enabled.
    """

    def test_intraday_conversion_suppressed_by_default(self, loop):
        """Position would qualify for conversion at the daily activation
        threshold, but intraday=True + config flag off → SKIPPED."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)
        live_quote = MagicMock(return_value=103.0)

        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0      # threshold $102
            cfg.risk.trailing_stop_trail_atr = 2.0
            cfg.risk.intraday_trail_conversion_enabled = False
            cfg.risk.intraday_conversion_buffer_atr = 0.5
            actions = mgr.manage(price_source=live_quote, intraday=True)

        assert actions[0].action == "SKIPPED"
        assert "ratchet-only" in actions[0].reason.lower() or \
               "conversions disabled" in actions[0].reason.lower()
        # No cancel / place attempted in ratchet-only mode.
        ibkr.cancel_order.assert_not_called()
        ibkr.place_trailing_stop.assert_not_called()

    def test_intraday_conversion_requires_buffer_above_activation(self, loop):
        """With conversions enabled, intraday needs activation_atr + buffer ATRs.
        Daily-runner-qualifying $103 sits between daily threshold $102 and
        intraday threshold $103 (entry $100 + (1.0 + 0.5)×$2 = $103) — equal
        is not strictly greater, so still SKIPPED.  Step up to $103.50."""
        ibkr = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr = TrailingStopManager(ibkr, loop)

        # Sub-threshold: $102.99 fails the intraday threshold $103.
        live_quote = MagicMock(return_value=102.99)
        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            cfg.risk.intraday_trail_conversion_enabled = True
            cfg.risk.intraday_conversion_buffer_atr = 0.5
            actions = mgr.manage(price_source=live_quote, intraday=True)
        assert actions[0].action == "SKIPPED"
        assert "below activation" in actions[0].reason

        # Step up: $103.50 clears the intraday threshold.
        live_quote2 = MagicMock(return_value=103.50)
        ibkr2 = _ibkr_stub(
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
            open_orders=_bracket_orders("AAPL"),
        )
        mgr2 = TrailingStopManager(ibkr2, loop)
        with (
            patch("risk.trailing_stop.config") as cfg,
            patch("data.database.get_latest_indicators", return_value={"atr_14": 2.0}),
        ):
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            cfg.risk.intraday_trail_conversion_enabled = True
            cfg.risk.intraday_conversion_buffer_atr = 0.5
            actions2 = mgr2.manage(price_source=live_quote2, intraday=True)
        assert actions2[0].action == "CONVERTED"


class TestIntradayRunLogSchema:
    """Phase 1 verification: the new intraday_run_log table is created on
    first DB engine init and exposes the expected columns."""

    def test_intraday_run_log_table_exists_with_columns(self, tmp_path, monkeypatch):
        """Fresh DB → create_all + _migrate run → intraday_run_log present."""
        from config.settings import config as _cfg
        import data.database as db

        # Point the engine at a throwaway DB and reset the singleton so this
        # test creates its own fresh engine.
        monkeypatch.setattr(_cfg.data, "db_path", str(tmp_path / "test.db"))
        monkeypatch.setattr(db, "_engine", None)
        engine = db.get_engine()

        from sqlalchemy import inspect
        insp = inspect(engine)
        assert "intraday_run_log" in insp.get_table_names()

        cols = {c["name"] for c in insp.get_columns("intraday_run_log")}
        expected = {
            "run_id", "run_timestamp", "mode", "status",
            "daily_loss_pct", "weekly_loss_pct", "cb_tripped",
            "positions_flattened", "trailing_evaluated",
            "trailing_ratcheted", "trailing_converted",
            "duration_seconds", "error_message",
        }
        missing = expected - cols
        assert not missing, f"Missing columns in intraday_run_log: {missing}"

    def test_log_intraday_run_round_trip(self, tmp_path, monkeypatch):
        """Schema is wired correctly end-to-end: write + read returns the row."""
        from config.settings import config as _cfg
        import data.database as db
        from datetime import datetime, timezone

        monkeypatch.setattr(_cfg.data, "db_path", str(tmp_path / "test.db"))
        monkeypatch.setattr(db, "_engine", None)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.log_intraday_run({
            "run_id":              "abc-123",
            "run_timestamp":       now,
            "mode":                "intraday",
            "status":              "completed",
            "daily_loss_pct":      -0.01,
            "weekly_loss_pct":     -0.02,
            "cb_tripped":          0,
            "positions_flattened": 0,
            "trailing_evaluated":  3,
            "trailing_ratcheted":  1,
            "trailing_converted":  0,
            "duration_seconds":    2.4,
            "error_message":       None,
        })

        df = db.get_intraday_run_log(limit=10)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["run_id"] == "abc-123"
        assert row["status"] == "completed"
        assert row["trailing_ratcheted"] == 1
        assert row["mode"] == "intraday"
