"""
Unit tests for risk/trailing_stop.py (Phase 3.5).

Uses AsyncMock for the IBKR async methods and a real asyncio event loop so
`run_until_complete(...)` drives the AsyncMocks correctly.  No live TWS
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

        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = True
            cfg.risk.trailing_stop_activation_atr = 1.0
            cfg.risk.trailing_stop_trail_atr = 2.0
            actions = mgr.manage()

        assert len(actions) == 1
        assert actions[0].action == "SKIPPED"
        assert "already active" in actions[0].reason
        ibkr.cancel_order.assert_not_called()
        ibkr.place_trailing_stop.assert_not_called()

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
