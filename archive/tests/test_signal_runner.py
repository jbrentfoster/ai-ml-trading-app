"""
Unit tests for signal_runner.py — dedup logic, stale-bar gate, and CB auto-trigger.

No live connections, yfinance, or database calls required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _restore_event_loop():
    """Restore a usable thread event loop after each test.

    Functions exercised here (`_phase4_risk_orders`, `_phase3_6_hold_timeouts`,
    …) legitimately call `asyncio.set_event_loop(None)` in their teardown — the
    correct production behaviour after closing a per-run loop.  That leaks
    across tests: a later test whose code path touches `get_event_loop()`
    (e.g. ib_insync / eventkit at import) then raises "no current event loop"
    on Python ≥ 3.12.  Re-seed a fresh loop after every test so order can't
    matter.
    """
    yield
    # Unconditional re-seed (cheap; orphaned loops are GC'd) — avoids the
    # get_event_loop() deprecation probe while guaranteeing the next test
    # starts with a valid current loop.
    asyncio.set_event_loop(asyncio.new_event_loop())

from scripts.signal_runner import (
    EQUIVALENT_PAIRS,
    _check_loss_limits_against_baseline,
    _fetch_held_long_symbols,
    _fetch_pending_entry_symbols,
    _phase3_6_hold_timeouts,
    _phase3_signals,
    _phase4_risk_orders,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_signal(symbol: str, signal: str = "BUY") -> SimpleNamespace:
    """Return a minimal signal_result-like object."""
    return SimpleNamespace(symbol=symbol, signal=signal, ensemble_score=0.8, passed_gate=True)


def _make_signal_result(
    symbol: str,
    signal: str = "HOLD",
    passed_gate: bool = False,
    ensemble_score: float = 0.0,
    gate_reason: str = "",
) -> SimpleNamespace:
    """SignalResult-shaped mock with every field log_signal reads."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return SimpleNamespace(
        symbol=symbol,
        signal=signal,
        passed_gate=passed_gate,
        ensemble_score=ensemble_score,
        gate_reason=gate_reason,
        bar_timestamp=now,
        generated_at=now,
        lstm_score=0.0,
        xgb_score=0.0,
        finbert_score=0.0,
        regime=SimpleNamespace(value="MEAN_REVERTING"),
    )


def _make_decision(symbol: str, decision: str = "DRY_RUN", signal: str = "BUY") -> MagicMock:
    """Return a mock OrderDecision."""
    d = MagicMock()
    d.symbol   = symbol
    d.decision = decision
    d.signal   = signal
    d.shares   = 10
    d.entry_price = 100.0
    d.reject_reason = None
    return d


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEquivalentPairs:
    def test_pairs_are_symmetric(self):
        """Every key's value must also map back to the key."""
        for sym, equiv in EQUIVALENT_PAIRS.items():
            assert EQUIVALENT_PAIRS.get(equiv) == sym, (
                f"EQUIVALENT_PAIRS['{equiv}'] should be '{sym}', got {EQUIVALENT_PAIRS.get(equiv)!r}"
            )

    def test_goog_googl_present(self):
        assert "GOOG"  in EQUIVALENT_PAIRS
        assert "GOOGL" in EQUIVALENT_PAIRS


class TestPhase4Deduplication:
    """Tests for within-session duplicate skipping in _phase4_risk_orders."""

    def _run(self, actionable, decisions):
        """
        Patch OrderManager so its process() returns decisions in order,
        then call _phase4_risk_orders and return
        (approved, dry_run_logged, rejected, skipped_duplicates,
        skipped_pending_orders, longs_closed).  dry_run=True ⇒ no IBKR
        connection, so skipped_pending_orders is always 0 here.
        """
        mock_mgr = MagicMock()
        mock_mgr.process.side_effect = decisions

        with patch("scripts.signal_runner.OrderManager", return_value=mock_mgr):
            return _phase4_risk_orders(
                actionable=actionable,
                equity=100_000,
                run_id="test-run-id",
                dry_run=True,
            )

    def test_no_duplicates_returns_all_submitted(self):
        """When no equivalent pairs appear, all actionable signals are processed."""
        actionable = [
            (_make_signal("AAPL"), 2.5),
            (_make_signal("MSFT"), 3.0),
        ]
        decisions = [
            _make_decision("AAPL"),
            _make_decision("MSFT"),
        ]
        approved, dry_run_logged, rejected, skipped, skipped_pending, longs_closed = self._run(
            actionable, decisions
        )
        assert approved       == 0
        assert dry_run_logged == 2
        assert rejected       == 0
        assert skipped        == 0

    def test_googl_skipped_when_goog_decided_first(self):
        """GOOGL must be skipped if GOOG was already processed this run."""
        actionable = [
            (_make_signal("GOOG"),  1.5),
            (_make_signal("GOOGL"), 1.5),
        ]
        # Only one decision will be consumed because GOOGL is skipped.
        decisions = [_make_decision("GOOG")]
        approved, dry_run_logged, rejected, skipped, skipped_pending, longs_closed = self._run(
            actionable, decisions
        )
        assert approved       == 0
        assert dry_run_logged == 1
        assert rejected       == 0
        assert skipped        == 1

    def test_goog_skipped_when_googl_decided_first(self):
        """GOOG must be skipped if GOOGL was already processed this run."""
        actionable = [
            (_make_signal("GOOGL"), 1.5),
            (_make_signal("GOOG"),  1.5),
        ]
        decisions = [_make_decision("GOOGL")]
        approved, dry_run_logged, rejected, skipped, skipped_pending, longs_closed = self._run(
            actionable, decisions
        )
        assert approved       == 0
        assert dry_run_logged == 1
        assert rejected       == 0
        assert skipped        == 1

    def test_rejected_symbol_still_blocks_equivalent(self):
        """A REJECTED decision still marks the symbol as decided, blocking its equivalent."""
        actionable = [
            (_make_signal("GOOG"),  1.5),
            (_make_signal("GOOGL"), 1.5),
        ]
        rejected_decision = _make_decision("GOOG", decision="REJECTED")
        rejected_decision.reject_reason = "portfolio drawdown exceeded"
        decisions = [rejected_decision]
        approved, dry_run_logged, rejected, skipped, skipped_pending, longs_closed = self._run(
            actionable, decisions
        )
        assert approved       == 0
        assert dry_run_logged == 0
        assert rejected       == 1
        assert skipped        == 1

    def test_rejected_no_position_counts_toward_rejected_total(self):
        """REJECTED_NO_POSITION (long-only gate firing on SELL-from-flat) must
        contribute to the rejected counter so signal_runner_log.orders_rejected
        matches what `order_decisions` shows (Page 8 / daily-review parity).
        Regression: 2026-05-12 run had 17 REJECTED_NO_POSITION rows but the
        Phase 5 summary printed "Orders rejected: 2"."""
        actionable = [
            (_make_signal("AAPL", signal="SELL"), 2.5),
            (_make_signal("MSFT", signal="SELL"), 3.0),
            (_make_signal("GOOG", signal="SELL"), 1.5),
        ]
        decisions = [
            _make_decision("AAPL", decision="REJECTED_NO_POSITION", signal="SELL"),
            _make_decision("MSFT", decision="REJECTED_NO_POSITION", signal="SELL"),
            _make_decision("GOOG", decision="REJECTED_NO_POSITION", signal="SELL"),
        ]
        approved, dry_run_logged, rejected, skipped, skipped_pending, longs_closed = self._run(
            actionable, decisions
        )
        assert approved       == 0
        assert dry_run_logged == 0
        assert rejected       == 3
        assert skipped        == 0
        assert longs_closed   == 0


# ── Pending-entry-order dedup (Phase 4) ───────────────────────────────────────

class TestFetchPendingEntrySymbols:
    """
    _fetch_pending_entry_symbols returns symbols with a working (unfilled) open
    order at IBKR that are NOT currently held — the set Phase 4 skips so it
    can't stack a duplicate bracket on a still-working entry.
    """

    def _ibkr(self, open_orders):
        ibkr = MagicMock()
        ibkr.get_open_orders = MagicMock(return_value=_async_value(open_orders))
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)
        return ibkr, loop

    def test_unfilled_unheld_entry_is_pending(self):
        """A working BUY entry on a not-held symbol is returned."""
        ibkr, loop = self._ibkr([
            {"symbol": "HPE", "action": "BUY", "order_type": "LMT", "remaining": 837},
        ])
        assert _fetch_pending_entry_symbols(ibkr, loop, {}) == {"HPE"}

    def test_held_symbol_excluded(self):
        """Protective TP/STP legs on a held position are NOT pending entries."""
        ibkr, loop = self._ibkr([
            {"symbol": "ARM", "action": "SELL", "order_type": "LMT", "remaining": 109},
            {"symbol": "ARM", "action": "SELL", "order_type": "STP", "remaining": 109},
        ])
        held = {"ARM": {"shares": 109}}
        assert _fetch_pending_entry_symbols(ibkr, loop, held) == set()

    def test_fully_filled_order_excluded(self):
        """remaining == 0 (nothing left to fill) is not a working entry."""
        ibkr, loop = self._ibkr([
            {"symbol": "HPE", "action": "BUY", "order_type": "LMT", "remaining": 0},
        ])
        assert _fetch_pending_entry_symbols(ibkr, loop, {}) == set()

    def test_unknown_remaining_treated_as_working(self):
        """Missing `remaining` defaults to working (conservative)."""
        ibkr, loop = self._ibkr([
            {"symbol": "LRCX", "action": "BUY", "order_type": "LMT"},
        ])
        assert _fetch_pending_entry_symbols(ibkr, loop, {}) == {"LRCX"}

    def test_no_ibkr_returns_empty(self):
        """Dry-run / paper-disabled (ibkr None) → empty set, no crash."""
        assert _fetch_pending_entry_symbols(None, None, {}) == set()

    def test_fetch_exception_returns_empty(self):
        """get_open_orders raising → empty set (best-effort guard, never blocks)."""
        async def _raise():
            raise RuntimeError("API error")
        ibkr = MagicMock()
        ibkr.get_open_orders = MagicMock(return_value=_raise())
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)
        assert _fetch_pending_entry_symbols(ibkr, loop, {}) == set()


class TestPhase4PendingOrderDedup:
    """
    Phase 4 must skip an actionable signal whose symbol already has an unfilled
    entry bracket working at IBKR — otherwise a second bracket is stacked on top
    (the duplicate guard only sees filled positions).
    """

    def _run(self, monkeypatch, actionable, decisions, positions, open_orders):
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", True)

        ibkr = MagicMock()
        ibkr.disconnect = MagicMock(return_value=_async_value(None))
        ibkr.get_positions = MagicMock(return_value=_async_value(positions))
        ibkr.get_open_orders = MagicMock(return_value=_async_value(open_orders))
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)

        mock_mgr = MagicMock()
        mock_mgr.process.side_effect = decisions

        with patch("scripts.signal_runner._connect_ibkr_if_needed",
                   return_value=(ibkr, loop)), \
             patch("scripts.signal_runner.OrderManager", return_value=mock_mgr):
            result = _phase4_risk_orders(
                actionable=actionable, equity=100_000,
                run_id="test-run", dry_run=False,
            )
        return result, mock_mgr

    def test_pending_entry_skips_new_buy(self, monkeypatch):
        """HPE has an unfilled BUY entry → today's HPE BUY is skipped, not sized."""
        actionable = [(_make_signal("HPE"), 2.0)]
        open_orders = [
            {"symbol": "HPE", "action": "BUY", "order_type": "LMT", "remaining": 837},
        ]
        (approved, dry_run_logged, rejected, skipped_dup, skipped_pending,
         longs_closed), mgr = self._run(
            monkeypatch, actionable, decisions=[], positions=[], open_orders=open_orders,
        )
        assert skipped_pending == 1
        assert approved == 0 and dry_run_logged == 0 and rejected == 0
        mgr.process.assert_not_called()

    def test_held_symbol_with_protective_legs_not_skipped(self, monkeypatch):
        """A held symbol's SELL TP/STP legs must NOT block a new decision —
        that path belongs to the duplicate guard / long-only close."""
        actionable = [(_make_signal("ARM"), 2.0)]
        positions = [{"symbol": "ARM", "quantity": 109, "avg_cost": 300.0}]
        open_orders = [
            {"symbol": "ARM", "action": "SELL", "order_type": "LMT", "remaining": 109},
            {"symbol": "ARM", "action": "SELL", "order_type": "STP", "remaining": 109},
        ]
        (approved, dry_run_logged, rejected, skipped_dup, skipped_pending,
         longs_closed), mgr = self._run(
            monkeypatch, actionable, decisions=[_make_decision("ARM")],
            positions=positions, open_orders=open_orders,
        )
        assert skipped_pending == 0
        assert dry_run_logged == 1
        mgr.process.assert_called_once()

    def test_unrelated_pending_does_not_skip(self, monkeypatch):
        """A pending entry on HPE must not block an unrelated AAPL signal."""
        actionable = [(_make_signal("AAPL"), 2.0)]
        open_orders = [
            {"symbol": "HPE", "action": "BUY", "order_type": "LMT", "remaining": 837},
        ]
        (approved, dry_run_logged, rejected, skipped_dup, skipped_pending,
         longs_closed), mgr = self._run(
            monkeypatch, actionable, decisions=[_make_decision("AAPL")],
            positions=[], open_orders=open_orders,
        )
        assert skipped_pending == 0
        assert dry_run_logged == 1
        mgr.process.assert_called_once()

    def test_equivalent_pending_entry_skips(self, monkeypatch):
        """A pending GOOG entry blocks a fresh GOOGL signal (and vice versa)."""
        actionable = [(_make_signal("GOOGL"), 2.0)]
        open_orders = [
            {"symbol": "GOOG", "action": "BUY", "order_type": "LMT", "remaining": 10},
        ]
        (approved, dry_run_logged, rejected, skipped_dup, skipped_pending,
         longs_closed), mgr = self._run(
            monkeypatch, actionable, decisions=[], positions=[], open_orders=open_orders,
        )
        assert skipped_pending == 1
        mgr.process.assert_not_called()


# ── Stale-bar gate ────────────────────────────────────────────────────────────

def _make_df(latest_date) -> pd.DataFrame:
    """Build a 60-row daily OHLCV-shaped frame with the given latest date."""
    idx = pd.date_range(end=pd.Timestamp(latest_date), periods=60, freq="D")
    return pd.DataFrame(
        {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000},
        index=idx,
    )


class TestStaleBarGate:
    """_phase3_signals must drop symbols whose newest bar is too old."""

    def _patch_engine(self, df_by_sym):
        engine = MagicMock()
        engine.run.side_effect = lambda sym, interval="1d": df_by_sym.get(sym, pd.DataFrame())
        return engine

    def test_fresh_bars_pass_through(self):
        """Bars from today are processed normally."""
        today = datetime.now(timezone.utc).date()
        fresh = {"AAPL": _make_df(today)}

        ensemble = MagicMock()
        ensemble.predict.return_value = pd.Series([0.0])
        gate = MagicMock()
        gate.evaluate.return_value = _make_signal_result(
            "AAPL", signal="HOLD", passed_gate=False,
            ensemble_score=0.1, gate_reason="below threshold",
        )

        with patch("scripts.signal_runner._load_ensemble", return_value=ensemble), \
             patch("data.indicators.IndicatorEngine", return_value=self._patch_engine(fresh)), \
             patch("models.signal_gate.SignalGate", return_value=gate), \
             patch("data.database.get_latest_indicators", return_value={"atr_14": 1.5}), \
             patch("scripts.signal_runner.log_signal"):
            actionable, skipped_stale = _phase3_signals(["AAPL"])

        assert skipped_stale == 0
        # Gate evaluated → no skip
        gate.evaluate.assert_called_once()

    def test_stale_bars_dropped(self):
        """Bars older than max_bar_staleness_days trip the gate."""
        from config.settings import config as cfg
        old_limit = cfg.risk.max_bar_staleness_days
        cfg.risk.max_bar_staleness_days = 3
        try:
            ten_days_ago = datetime.now(timezone.utc).date() - timedelta(days=10)
            stale = {"AAPL": _make_df(ten_days_ago)}

            ensemble = MagicMock()
            gate = MagicMock()

            with patch("scripts.signal_runner._load_ensemble", return_value=ensemble), \
                 patch("data.indicators.IndicatorEngine", return_value=self._patch_engine(stale)), \
                 patch("models.signal_gate.SignalGate", return_value=gate):
                actionable, skipped_stale = _phase3_signals(["AAPL"])

            assert skipped_stale == 1
            assert actionable == []
            # Stale → never reaches predict / evaluate
            ensemble.predict.assert_not_called()
            gate.evaluate.assert_not_called()
        finally:
            cfg.risk.max_bar_staleness_days = old_limit


# ── Circuit breaker auto-trigger ──────────────────────────────────────────────

class TestCBAutoTrigger:
    """_check_loss_limits_against_baseline drives CircuitBreaker.check_loss_limits()."""

    def _enable_paper(self, monkeypatch):
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", True)

    def test_no_baseline_seeds_snapshot_only(self, monkeypatch):
        """First-ever run: no prior snapshot → CB not invoked, snapshot written."""
        self._enable_paper(monkeypatch)

        cb = MagicMock()
        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(True))
        ibkr.get_account_summary = MagicMock(return_value=_async_value(
            SimpleNamespace(net_liquidation=100_000, total_cash=20_000,
                            unrealized_pnl=0.0, realized_pnl=0.0)
        ))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr), \
             patch("scripts.signal_runner.get_equity_snapshot_on_or_before", return_value=None), \
             patch("scripts.signal_runner.log_equity_snapshot") as mock_log:
            halted, _ = _check_loss_limits_against_baseline(cb)

        assert halted is False
        cb.check_loss_limits.assert_not_called()
        mock_log.assert_called_once()

    def test_loss_within_limits_does_not_halt(self, monkeypatch):
        """Day-over-day loss under threshold → CB called, returns False."""
        self._enable_paper(monkeypatch)

        cb = MagicMock()
        cb.check_loss_limits.return_value = False

        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(True))
        ibkr.get_account_summary = MagicMock(return_value=_async_value(
            SimpleNamespace(net_liquidation=99_000, total_cash=20_000,
                            unrealized_pnl=-1_000, realized_pnl=0.0)
        ))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))

        baseline = {"net_liquidation": 100_000}

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr), \
             patch("scripts.signal_runner.get_equity_snapshot_on_or_before",
                   return_value=baseline), \
             patch("scripts.signal_runner.log_equity_snapshot"):
            halted, _ = _check_loss_limits_against_baseline(cb)

        assert halted is False
        cb.check_loss_limits.assert_called_once()
        daily_pct, weekly_pct = cb.check_loss_limits.call_args[0]
        assert daily_pct == pytest.approx(-0.01, abs=1e-6)
        assert weekly_pct == pytest.approx(-0.01, abs=1e-6)

    def test_loss_breaches_threshold_triggers_halt(self, monkeypatch):
        """5% daily loss breaches default 3% threshold → halted=True."""
        self._enable_paper(monkeypatch)

        cb = MagicMock()
        cb.check_loss_limits.return_value = True
        cb.is_halted.return_value = (True, "Daily loss 5.0% exceeds 3.0%")

        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(True))
        ibkr.get_account_summary = MagicMock(return_value=_async_value(
            SimpleNamespace(net_liquidation=95_000, total_cash=10_000,
                            unrealized_pnl=-5_000, realized_pnl=0.0)
        ))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))

        baseline = {"net_liquidation": 100_000}

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr), \
             patch("scripts.signal_runner.get_equity_snapshot_on_or_before",
                   return_value=baseline), \
             patch("scripts.signal_runner.log_equity_snapshot"):
            halted, reason = _check_loss_limits_against_baseline(cb)

        assert halted is True
        assert "Daily loss" in reason

    def test_ibkr_unreachable_skips_check(self, monkeypatch):
        """connect() returning False → no CB call, no exception."""
        self._enable_paper(monkeypatch)

        cb = MagicMock()
        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(False))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr), \
             patch("scripts.signal_runner.log_equity_snapshot") as mock_log:
            halted, _ = _check_loss_limits_against_baseline(cb)

        assert halted is False
        cb.check_loss_limits.assert_not_called()
        mock_log.assert_not_called()

    def test_paper_disabled_skips_check(self, monkeypatch):
        """paper_orders_enabled=False → IBKR never opened, CB not called."""
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", False)

        cb = MagicMock()
        with patch("execution.ibkr_connection.IBKRConnection") as mock_ibkr:
            halted, _ = _check_loss_limits_against_baseline(cb)
        assert halted is False
        cb.check_loss_limits.assert_not_called()
        mock_ibkr.assert_not_called()


def _async_value(value):
    """Wrap a value in a coroutine so it works with loop.run_until_complete."""
    async def _coro():
        return value
    return _coro()


# ── Held-position override (orphan-position guard) ────────────────────────────

class TestHeldLongSymbols:
    """
    _fetch_held_long_symbols ensures held longs are tracked even after a
    universe rescore drops them — without it the trailing-stop manager
    evaluates against a stale cached close.
    """

    def _enable_paper(self, monkeypatch):
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", True)

    def test_returns_long_position_symbols(self, monkeypatch):
        """Held longs (shares > 0) are included; flats and shorts are not."""
        self._enable_paper(monkeypatch)

        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(True))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))
        ibkr.get_positions = MagicMock(return_value=_async_value([
            {"symbol": "TMUS", "quantity": 204, "avg_cost": 195.41},
            {"symbol": "WFC",  "quantity": 500, "avg_cost":  79.94},
            {"symbol": "FLAT", "quantity":   0, "avg_cost":  10.00},
            {"symbol": "SHORT", "quantity": -100, "avg_cost":  50.00},
        ]))

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr):
            held = _fetch_held_long_symbols()

        assert held == {"TMUS", "WFC"}

    def test_paper_disabled_returns_empty(self, monkeypatch):
        """No IBKR connection attempted when paper_orders_enabled=False."""
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", False)

        with patch("execution.ibkr_connection.IBKRConnection") as mock_ibkr:
            held = _fetch_held_long_symbols()

        assert held == set()
        mock_ibkr.assert_not_called()

    def test_connect_failure_returns_empty(self, monkeypatch):
        """connect() returning False → empty set, no exception."""
        self._enable_paper(monkeypatch)

        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(False))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr):
            held = _fetch_held_long_symbols()

        assert held == set()
        ibkr.get_positions.assert_not_called()

    def test_get_positions_exception_returns_empty(self, monkeypatch):
        """get_positions raising → empty set, no exception bubbles up."""
        self._enable_paper(monkeypatch)

        async def _raise():
            raise RuntimeError("API error")

        ibkr = MagicMock()
        ibkr.connect = MagicMock(return_value=_async_value(True))
        ibkr.disconnect = MagicMock(return_value=_async_value(None))
        ibkr.get_positions = MagicMock(return_value=_raise())

        with patch("execution.ibkr_connection.IBKRConnection", return_value=ibkr):
            held = _fetch_held_long_symbols()

        assert held == set()


# ── signal_log persistence ────────────────────────────────────────────────────

class TestSignalLogPersistence:
    """
    _phase3_signals must write every SignalResult to signal_log so Page 3's
    score-history view reflects the daily runner's output (HOLD / BUY / SELL,
    passed or failed gate).
    """

    def _patch_engine(self, df_by_sym):
        engine = MagicMock()
        engine.run.side_effect = lambda sym, interval="1d": df_by_sym.get(sym, pd.DataFrame())
        return engine

    def test_log_signal_called_for_passed_gate(self):
        """A BUY that passes the gate is persisted with passed_gate=True."""
        today = datetime.now(timezone.utc).date()
        fresh = {"AAPL": _make_df(today)}

        ensemble = MagicMock()
        ensemble.predict.return_value = {"lstm": 0.7, "xgb": 0.6, "finbert": 0.5, "ensemble": 0.65}
        gate = MagicMock()
        gate.evaluate.return_value = _make_signal_result(
            "AAPL", signal="BUY", passed_gate=True, ensemble_score=0.65,
        )

        with patch("scripts.signal_runner._load_ensemble", return_value=ensemble), \
             patch("data.indicators.IndicatorEngine", return_value=self._patch_engine(fresh)), \
             patch("models.signal_gate.SignalGate", return_value=gate), \
             patch("data.database.get_latest_indicators", return_value={"atr_14": 1.5}), \
             patch("scripts.signal_runner.log_signal") as mock_log:
            _phase3_signals(["AAPL"])

        mock_log.assert_called_once()
        record = mock_log.call_args[0][0]
        assert record["symbol"]      == "AAPL"
        assert record["signal"]      == "BUY"
        assert record["passed_gate"] is True
        assert record["regime"]      == "MEAN_REVERTING"
        # Field shape matches data/database.SignalLog columns
        for field in ("generated_at", "bar_timestamp", "lstm_score",
                      "xgb_score", "finbert_score", "ensemble_score",
                      "gate_reason"):
            assert field in record

    def test_log_signal_called_for_failed_gate(self):
        """HOLDs that fail the gate are still persisted (Page 3 needs them)."""
        today = datetime.now(timezone.utc).date()
        fresh = {"AAPL": _make_df(today)}

        ensemble = MagicMock()
        ensemble.predict.return_value = {"lstm": 0.1, "xgb": 0.1, "finbert": 0.0, "ensemble": 0.07}
        gate = MagicMock()
        gate.evaluate.return_value = _make_signal_result(
            "AAPL", signal="HOLD", passed_gate=False,
            ensemble_score=0.07, gate_reason="Filter1 fail: |0.07| < threshold 0.50",
        )

        with patch("scripts.signal_runner._load_ensemble", return_value=ensemble), \
             patch("data.indicators.IndicatorEngine", return_value=self._patch_engine(fresh)), \
             patch("models.signal_gate.SignalGate", return_value=gate), \
             patch("data.database.get_latest_indicators", return_value={"atr_14": 1.5}), \
             patch("scripts.signal_runner.log_signal") as mock_log:
            _phase3_signals(["AAPL"])

        mock_log.assert_called_once()
        record = mock_log.call_args[0][0]
        assert record["signal"]      == "HOLD"
        assert record["passed_gate"] is False
        assert "Filter1 fail" in record["gate_reason"]

    def test_log_signal_skipped_when_gate_raises(self):
        """If gate.evaluate raises, log_signal is NOT called (no partial row)."""
        today = datetime.now(timezone.utc).date()
        fresh = {"AAPL": _make_df(today)}

        ensemble = MagicMock()
        ensemble.predict.return_value = {"lstm": 0.0, "xgb": 0.0, "finbert": 0.0, "ensemble": 0.0}
        gate = MagicMock()
        gate.evaluate.side_effect = RuntimeError("regime detector blew up")

        with patch("scripts.signal_runner._load_ensemble", return_value=ensemble), \
             patch("data.indicators.IndicatorEngine", return_value=self._patch_engine(fresh)), \
             patch("models.signal_gate.SignalGate", return_value=gate), \
             patch("scripts.signal_runner.log_signal") as mock_log:
            _phase3_signals(["AAPL"])

        mock_log.assert_not_called()


# ── Hold timeout (Phase 3.6) ──────────────────────────────────────────────────

class TestHoldTimeout:
    """
    _phase3_6_hold_timeouts flattens held longs whose most recent passed-gate
    BUY signal is older than `config.risk.max_hold_days`.  Opt-in via
    `config.risk.hold_timeout_enabled`; skipped in dry-run / when
    `paper_orders_enabled=False`.
    """

    def _enable(self, monkeypatch, max_days: int = 30):
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", True)
        monkeypatch.setattr(cfg.risk, "hold_timeout_enabled", True)
        monkeypatch.setattr(cfg.risk, "max_hold_days", max_days)

    def _mock_ibkr(self, positions, open_orders=None):
        """Build an ibkr mock returning the given positions + open_orders."""
        ibkr = MagicMock()
        ibkr.disconnect = MagicMock(return_value=_async_value(None))
        ibkr.get_positions = MagicMock(return_value=_async_value(positions))
        ibkr.get_open_orders = MagicMock(return_value=_async_value(open_orders or []))
        ibkr.place_market_order = MagicMock(return_value=_async_value(None))
        ibkr.cancel_order = MagicMock(return_value=_async_value(True))
        return ibkr

    def test_disabled_by_default_is_noop(self, monkeypatch):
        """hold_timeout_enabled=False → returns 0, no IBKR connect."""
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", True)
        monkeypatch.setattr(cfg.risk, "hold_timeout_enabled", False)

        with patch("scripts.signal_runner._connect_ibkr_if_needed") as mock_conn:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")

        assert count == 0
        mock_conn.assert_not_called()

    def test_dry_run_is_noop(self, monkeypatch):
        """dry_run=True → returns 0 even if hold_timeout_enabled=True."""
        self._enable(monkeypatch)
        with patch("scripts.signal_runner._connect_ibkr_if_needed") as mock_conn:
            count = _phase3_6_hold_timeouts(dry_run=True, run_id="x")
        assert count == 0
        mock_conn.assert_not_called()

    def test_paper_disabled_is_noop(self, monkeypatch):
        """paper_orders_enabled=False → returns 0, no IBKR connect."""
        from config.settings import config as cfg, TradingMode
        monkeypatch.setattr(cfg.trading, "mode", TradingMode.SIMULATION)
        monkeypatch.setattr(cfg.trading, "paper_orders_enabled", False)
        monkeypatch.setattr(cfg.risk, "hold_timeout_enabled", True)
        monkeypatch.setattr(cfg.risk, "max_hold_days", 30)

        with patch("scripts.signal_runner._connect_ibkr_if_needed") as mock_conn:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")
        assert count == 0
        mock_conn.assert_not_called()

    def test_zero_max_days_is_noop(self, monkeypatch):
        """max_hold_days=0 → returns 0 (defensive — would flatten everything)."""
        self._enable(monkeypatch, max_days=0)
        with patch("scripts.signal_runner._connect_ibkr_if_needed") as mock_conn:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")
        assert count == 0
        mock_conn.assert_not_called()

    def test_recent_buy_blocks_timeout(self, monkeypatch):
        """Held long with a BUY signal within the window is NOT closed."""
        self._enable(monkeypatch, max_days=30)
        positions = [{"symbol": "AAPL", "quantity": 100, "avg_cost": 150.0}]
        ibkr = self._mock_ibkr(positions)
        loop = MagicMock()
        # run_until_complete returns whatever coroutine result it was given —
        # mimic by unwrapping the coroutine value.
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)

        recent_buy = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
        with patch("scripts.signal_runner._connect_ibkr_if_needed",
                   return_value=(ibkr, loop)), \
             patch("scripts.signal_runner.get_latest_buy_signal_ts",
                   return_value=recent_buy), \
             patch("scripts.signal_runner.log_order_decision") as mock_log:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")

        assert count == 0
        ibkr.place_market_order.assert_not_called()
        mock_log.assert_not_called()

    def test_stale_buy_triggers_timeout(self, monkeypatch):
        """Held long with no BUY in `max_hold_days` is flattened + persisted."""
        self._enable(monkeypatch, max_days=30)
        positions = [{"symbol": "AAPL", "quantity": 100, "avg_cost": 150.0}]
        # bracket children that should be cancelled before the market close
        open_orders = [
            {"symbol": "AAPL", "action": "SELL", "order_type": "LMT", "order_id": 1},
            {"symbol": "AAPL", "action": "SELL", "order_type": "STP", "order_id": 2},
            # Unrelated leg should NOT be cancelled
            {"symbol": "MSFT", "action": "SELL", "order_type": "LMT", "order_id": 3},
        ]
        ibkr = self._mock_ibkr(positions, open_orders)
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)

        stale_buy = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=45)
        with patch("scripts.signal_runner._connect_ibkr_if_needed",
                   return_value=(ibkr, loop)), \
             patch("scripts.signal_runner.get_latest_buy_signal_ts",
                   return_value=stale_buy), \
             patch("scripts.signal_runner.log_order_decision") as mock_log:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="run-1")

        assert count == 1
        # Both AAPL bracket children cancelled, MSFT untouched
        cancel_ids = [
            call.args[0] for call in ibkr.cancel_order.call_args_list
        ]
        assert 1 in cancel_ids
        assert 2 in cancel_ids
        assert 3 not in cancel_ids
        # Market sell submitted for the right symbol + quantity
        ibkr.place_market_order.assert_called_once_with("AAPL", "SELL", 100)
        # Persisted with CLOSED_TIMEOUT
        mock_log.assert_called_once()
        record = mock_log.call_args[0][0]
        assert record["symbol"]   == "AAPL"
        assert record["decision"] == "CLOSED_TIMEOUT"
        assert record["signal"]   == "SELL"
        assert record["shares"]   == 100
        assert record["run_id"]   == "run-1"

    def test_no_buy_history_is_skipped(self, monkeypatch):
        """Held long with no BUY signal in signal_log is NOT closed (manual position)."""
        self._enable(monkeypatch, max_days=30)
        positions = [{"symbol": "AAPL", "quantity": 100, "avg_cost": 150.0}]
        ibkr = self._mock_ibkr(positions)
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)

        with patch("scripts.signal_runner._connect_ibkr_if_needed",
                   return_value=(ibkr, loop)), \
             patch("scripts.signal_runner.get_latest_buy_signal_ts",
                   return_value=None), \
             patch("scripts.signal_runner.log_order_decision") as mock_log:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")

        assert count == 0
        ibkr.place_market_order.assert_not_called()
        mock_log.assert_not_called()

    def test_short_positions_skipped(self, monkeypatch):
        """Negative-quantity positions are filtered out before timeout eval."""
        self._enable(monkeypatch, max_days=30)
        positions = [
            {"symbol": "AAPL", "quantity": -100, "avg_cost": 150.0},  # short
            {"symbol": "FLAT", "quantity":    0, "avg_cost":  50.0},  # flat
        ]
        ibkr = self._mock_ibkr(positions)
        loop = MagicMock()
        loop.run_until_complete.side_effect = lambda coro: _run_coro(coro)

        with patch("scripts.signal_runner._connect_ibkr_if_needed",
                   return_value=(ibkr, loop)), \
             patch("scripts.signal_runner.get_latest_buy_signal_ts") as mock_q, \
             patch("scripts.signal_runner.log_order_decision") as mock_log:
            count = _phase3_6_hold_timeouts(dry_run=False, run_id="x")

        assert count == 0
        # Should not have even asked about BUY history for short / flat
        mock_q.assert_not_called()
        ibkr.place_market_order.assert_not_called()
        mock_log.assert_not_called()


def _run_coro(coro):
    """Drain a coroutine synchronously for tests (mock loop)."""
    import asyncio as _aio
    try:
        return _aio.get_event_loop().run_until_complete(coro)
    except RuntimeError:
        loop = _aio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
