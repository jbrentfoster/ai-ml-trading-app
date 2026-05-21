"""
Unit tests for the risk package (Phase 4).

18 tests covering:
  PositionSizer   (6)
  PortfolioGuard  (6)
  CircuitBreaker  (5)
  OrderManager    (4)

All tests use in-memory SQLite (via tmp_path monkeypatch) or
unittest.mock.patch — no live network or IBKR connections required.
"""

from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


# ── Shared in-memory DB fixture ───────────────────────────────────────────────

@pytest.fixture()
def mem_engine(monkeypatch, tmp_path):
    """
    Replace get_engine() with an in-memory SQLite engine for the duration of
    each test.  Ensures tables are created fresh.
    """
    from data.database import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────────────
# PositionSizer
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionSizer:

    def _sizer(self):
        from risk.position_sizer import PositionSizer
        return PositionSizer()

    def test_kelly_insufficient_history_uses_fixed(self, mem_engine):
        """With fewer than kelly_min_trades past signals, method=='fixed'."""
        sizer = self._sizer()
        # No signal_log rows → falls back to fixed sizing
        result = sizer.calculate("AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0)
        assert result.method == "fixed"
        assert result.shares >= 0

    def test_kelly_calculation_with_history(self, mem_engine):
        """With sufficient win/loss history, Kelly sizing is used."""
        from data.database import SignalLog

        now = _now()
        # Seed 15 past signals: 10 wins (score > 0), 5 losses (score < 0)
        with Session(mem_engine) as session:
            for i in range(10):
                session.add(SignalLog(
                    symbol="AAPL", generated_at=now, bar_timestamp=now,
                    ensemble_score=0.5, signal="BUY", passed_gate=True,
                ))
            for i in range(5):
                session.add(SignalLog(
                    symbol="AAPL", generated_at=now, bar_timestamp=now,
                    ensemble_score=-0.3, signal="SELL", passed_gate=True,
                ))
            session.commit()

        sizer = self._sizer()
        result = sizer.calculate("AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0)
        # Cold-start path uses signal_log proxy; realised Kelly requires trade_log history.
        assert result.method == "kelly_proxy"
        assert 0 < result.position_pct <= 0.10   # capped at kelly_max_position_pct

    def test_position_capped_at_max(self, mem_engine):
        """Kelly output > kelly_max_position_pct is capped."""
        from data.database import SignalLog

        now = _now()
        # Perfect win rate → raw Kelly will be very high
        with Session(mem_engine) as session:
            for _ in range(20):
                session.add(SignalLog(
                    symbol="TSLA", generated_at=now, bar_timestamp=now,
                    ensemble_score=1.0, signal="BUY", passed_gate=True,
                ))
            session.commit()

        sizer = self._sizer()
        result = sizer.calculate("TSLA", "BUY", equity=100_000, entry_price=200.0, atr=4.0)
        from config.settings import config
        assert result.position_pct <= config.risk.kelly_max_position_pct

    def test_atr_stop_placement_buy(self, mem_engine):
        """BUY stop = entry - ATR * multiplier."""
        from config.settings import config
        sizer = self._sizer()
        entry, atr = 200.0, 5.0
        result = sizer.calculate("AAPL", "BUY", equity=100_000, entry_price=entry, atr=atr)
        expected_stop = entry - atr * config.risk.atr_stop_multiplier
        assert abs(result.stop_price - expected_stop) < 0.01

    def test_atr_stop_placement_sell(self, mem_engine):
        """SELL stop = entry + ATR * multiplier."""
        from config.settings import config
        sizer = self._sizer()
        entry, atr = 200.0, 5.0
        result = sizer.calculate("AAPL", "SELL", equity=100_000, entry_price=entry, atr=atr)
        expected_stop = entry + atr * config.risk.atr_stop_multiplier
        assert abs(result.stop_price - expected_stop) < 0.01

    def test_fixed_stop_when_atr_zero(self, mem_engine):
        """ATR=0 falls back to fixed_stop_loss_pct stop."""
        from config.settings import config
        sizer = self._sizer()
        entry = 100.0
        result = sizer.calculate("AAPL", "BUY", equity=100_000, entry_price=entry, atr=0.0)
        expected_stop = entry * (1 - config.risk.fixed_stop_loss_pct)
        assert abs(result.stop_price - expected_stop) < 0.01

    def test_realised_kelly_history_used_when_threshold_met(self, mem_engine):
        """When ``kelly_history`` carries enough trades and a positive ``f_star``,
        the sizer reports ``method='kelly_realised'`` and the position is sized
        from Kelly (capped at the hard limit)."""
        from config.settings import config
        sizer = self._sizer()
        # Fabricate a realised history that clears min_trades_for_realised_kelly
        # with a healthy edge (60% win rate, b=1.5).
        kelly_history = {
            "n_trades":     max(config.risk.min_trades_for_realised_kelly, 30),
            "win_rate":     0.60,
            "avg_win_pct":  0.03,
            "avg_loss_pct": 0.02,
            "b":            1.5,
            "f_star":       (0.60 * 1.5 - 0.40) / 1.5,   # 0.333
        }
        result = sizer.calculate(
            "AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0,
            kelly_history=kelly_history,
        )
        assert result.method == "kelly_realised"
        assert 0 < result.position_pct <= config.risk.kelly_max_position_pct

    def test_realised_kelly_below_threshold_falls_back_to_proxy(self, mem_engine):
        """``n_trades < min_trades_for_realised_kelly`` falls back to the
        signal_log proxy (or fixed when signal_log is also empty)."""
        sizer = self._sizer()
        kelly_history = {
            "n_trades":     5,         # below default 30
            "win_rate":     0.6,
            "avg_win_pct":  0.03,
            "avg_loss_pct": 0.02,
            "b":            1.5,
            "f_star":       0.333,
        }
        result = sizer.calculate(
            "AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0,
            kelly_history=kelly_history,
        )
        # Empty signal_log → proxy falls through to fixed.
        assert result.method == "fixed"

    def test_realised_kelly_undefined_falls_back(self, mem_engine):
        """``f_star=None`` (e.g. all-wins or all-losses history) is treated as
        insufficient and falls back to the proxy/fixed path."""
        sizer = self._sizer()
        kelly_history = {
            "n_trades":     50,
            "win_rate":     1.0,
            "avg_win_pct":  0.03,
            "avg_loss_pct": 0.0,
            "b":            None,
            "f_star":       None,
        }
        result = sizer.calculate(
            "AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0,
            kelly_history=kelly_history,
        )
        assert result.method == "fixed"

    def test_realised_kelly_negative_fstar_floors_to_zero(self, mem_engine):
        """A negative ``f_star`` (lose-heavy history) yields ``position_pct=0``
        — the sizer never proposes a negative or below-zero position."""
        sizer = self._sizer()
        kelly_history = {
            "n_trades":     50,
            "win_rate":     0.30,
            "avg_win_pct":  0.01,
            "avg_loss_pct": 0.04,
            "b":            0.25,
            "f_star":       (0.30 * 0.25 - 0.70) / 0.25,   # negative
        }
        result = sizer.calculate(
            "AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0,
            kelly_history=kelly_history,
        )
        # Method label still reflects the realised path; pct is floored at 0.
        assert result.method == "kelly_realised"
        assert result.position_pct == 0.0
        assert result.shares == 0


# ─────────────────────────────────────────────────────────────────────────────
# compute_realised_kelly
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeRealisedKelly:
    """Phase C — realised-Kelly statistics from ``trade_log``."""

    def _seed_trade(
        self,
        engine,
        symbol: str = "AAPL",
        pnl_pct: float = 0.05,
        entry_ts: datetime | None = None,
        source: str = "walk_forward",
        run_id: str = "run-A",
        fold_index: int = 0,
    ) -> None:
        from data.database import TradeLog
        ts = entry_ts or _now()
        with Session(engine) as session:
            session.add(TradeLog(
                source=source,
                run_id=run_id,
                fold_index=fold_index,
                symbol=symbol,
                signal="BUY",
                entry_ts=ts,
                entry_px=100.0,
                exit_ts=ts + timedelta(days=1),
                exit_px=100.0 * (1 + pnl_pct),
                exit_reason="tp" if pnl_pct > 0 else "stop",
                shares=10.0,
                pnl=pnl_pct * 100.0 * 10.0,
                pnl_pct=pnl_pct,
                costs_charged=0.0,
                recorded_at=ts,
            ))
            session.commit()

    def test_returns_none_when_no_trades(self, mem_engine):
        from risk.position_sizer import compute_realised_kelly
        assert compute_realised_kelly("AAPL") is None

    def test_basic_win_loss_stats(self, mem_engine):
        """Mixed wins and losses produce sensible (p, b, f*)."""
        from risk.position_sizer import compute_realised_kelly
        # 6 wins at +5%, 4 losses at -2.5% → p=0.6, b=2.0, f*=(0.6*2-0.4)/2=0.4
        for _ in range(6):
            self._seed_trade(mem_engine, pnl_pct=0.05)
        for _ in range(4):
            self._seed_trade(mem_engine, pnl_pct=-0.025)

        out = compute_realised_kelly("AAPL")
        assert out is not None
        assert out["n_trades"]    == 10
        assert out["win_rate"]    == pytest.approx(0.6)
        assert out["avg_win_pct"] == pytest.approx(0.05)
        assert out["avg_loss_pct"] == pytest.approx(0.025)
        assert out["b"]           == pytest.approx(2.0)
        assert out["f_star"]      == pytest.approx(0.4)

    def test_forward_only_invariant_via_as_of(self, mem_engine):
        """``as_of`` filters out trades whose entry_ts is on or after the cutoff —
        the WF orchestrator relies on this to avoid lookahead."""
        from risk.position_sizer import compute_realised_kelly
        now = _now()
        # Two old wins (entry_ts < cutoff) and one future loss (entry_ts > cutoff).
        self._seed_trade(mem_engine, pnl_pct=+0.05, entry_ts=now - timedelta(days=10))
        self._seed_trade(mem_engine, pnl_pct=+0.05, entry_ts=now - timedelta(days=5))
        self._seed_trade(mem_engine, pnl_pct=-0.10, entry_ts=now + timedelta(days=1))

        out = compute_realised_kelly("AAPL", as_of=now)
        assert out is not None
        # Only the two old wins should be counted.
        assert out["n_trades"] == 2
        # All wins → Kelly undefined.
        assert out["f_star"]   is None

    def test_run_id_filter(self, mem_engine):
        """``run_id`` scopes the query so a fresh WF run cannot pick up trades
        from a previous run with different ensemble weights."""
        from risk.position_sizer import compute_realised_kelly
        for _ in range(5):
            self._seed_trade(mem_engine, pnl_pct=+0.05, run_id="run-OLD")
        for _ in range(3):
            self._seed_trade(mem_engine, pnl_pct=-0.025, run_id="run-NEW")

        out_new = compute_realised_kelly("AAPL", run_id="run-NEW")
        assert out_new is not None
        assert out_new["n_trades"] == 3

        out_old = compute_realised_kelly("AAPL", run_id="run-OLD")
        assert out_old is not None
        assert out_old["n_trades"] == 5

    def test_source_filter(self, mem_engine):
        """``source='live'`` excludes walk_forward rows so OrderManager sizes
        from real fills only, not WF backtest noise."""
        from risk.position_sizer import compute_realised_kelly
        self._seed_trade(mem_engine, source="walk_forward")
        self._seed_trade(mem_engine, source="walk_forward")
        self._seed_trade(mem_engine, source="live")

        out_live = compute_realised_kelly("AAPL", source="live")
        assert out_live is not None
        assert out_live["n_trades"] == 1

        out_wf = compute_realised_kelly("AAPL", source="walk_forward")
        assert out_wf is not None
        assert out_wf["n_trades"] == 2

    def test_all_wins_returns_undefined_kelly(self, mem_engine):
        """All-winners history → b/f_star=None; the sizer treats this as
        insufficient and falls back to the cold-start path."""
        from risk.position_sizer import compute_realised_kelly
        for _ in range(5):
            self._seed_trade(mem_engine, pnl_pct=+0.05)
        out = compute_realised_kelly("AAPL")
        assert out is not None
        assert out["n_trades"] == 5
        assert out["b"]        is None
        assert out["f_star"]   is None


# ─────────────────────────────────────────────────────────────────────────────
# PortfolioGuard
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioGuard:

    def _guard(self, mem_engine, cb_halted=False):
        from risk.circuit_breaker import CircuitBreaker
        from risk.portfolio_guard import PortfolioGuard

        cb = CircuitBreaker()
        if cb_halted:
            cb.trigger("test halt")

        return PortfolioGuard(circuit_breaker=cb)

    def _pos_size(self, value=5_000, entry=100.0):
        from risk.position_sizer import PositionSize
        return PositionSize(
            symbol="AAPL", signal="BUY", shares=50,
            entry_price=entry, stop_price=98.0, take_profit_price=106.0,
            position_value=value, position_pct=value / 100_000,
            kelly_fraction_used=0.05, method="fixed",
        )

    def test_all_checks_pass(self, mem_engine):
        guard = self._guard(mem_engine)
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(),
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert result.passed
        assert result.checks.get("circuit_breaker") is True

    def test_circuit_breaker_blocks(self, mem_engine):
        guard = self._guard(mem_engine, cb_halted=True)
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(),
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert "circuit breaker" in result.reason.lower()
        assert result.checks.get("circuit_breaker") is False

    def test_portfolio_drawdown_blocks(self, mem_engine):
        guard = self._guard(mem_engine)
        # daily loss of -15% exceeds 10% limit
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(),
            equity=100_000, positions={}, daily_pnl_pct=-0.15,
        )
        assert not result.passed
        assert result.checks.get("portfolio_drawdown") is False

    def test_position_size_too_large(self, mem_engine):
        guard = self._guard(mem_engine)
        # 8% position with 5% limit → blocked
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(value=8_000),
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("position_size") is False

    def test_duplicate_position_blocked(self, mem_engine):
        guard = self._guard(mem_engine)
        positions = {"AAPL": {"shares": 50, "entry_price": 100, "current_price": 105}}
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(),
            equity=100_000, positions=positions, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("no_duplicate") is False

    def test_buy_stop_on_wrong_side_blocked(self, mem_engine):
        """BUY with stop >= entry is rejected by stop_sanity check."""
        guard = self._guard(mem_engine)
        bad = self._pos_size()
        bad.stop_price = bad.entry_price + 1.0   # stop above entry — wrong side for BUY
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=bad,
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("stop_sanity") is False
        assert "wrong side" in result.reason.lower() or "must be below" in result.reason.lower()

    def test_sell_stop_on_wrong_side_blocked(self, mem_engine):
        """SELL with stop <= entry is rejected by stop_sanity check."""
        guard = self._guard(mem_engine)
        bad = self._pos_size()
        bad.stop_price = bad.entry_price - 1.0   # stop below entry — wrong side for SELL
        result = guard.check(
            symbol="AAPL", signal="SELL",
            position_size=bad,
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("stop_sanity") is False

    def test_stop_equal_to_entry_blocked(self, mem_engine):
        """stop_price == entry_price is rejected (zero stop distance)."""
        guard = self._guard(mem_engine)
        bad = self._pos_size()
        bad.stop_price = bad.entry_price
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=bad,
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("stop_sanity") is False

    def test_goog_googl_blocked(self, mem_engine):
        """Holding GOOG should block a new GOOGL position."""
        guard = self._guard(mem_engine)
        positions = {"GOOG": {"shares": 10, "entry_price": 170, "current_price": 175}}
        ps = self._pos_size()
        ps.symbol = "GOOGL"
        result = guard.check(
            symbol="GOOGL", signal="BUY",
            position_size=ps,
            equity=100_000, positions=positions, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("no_duplicate") is False

    # ── Sector exposure ──────────────────────────────────────────────────────

    def test_sector_exposure_blocks_when_over_cap(self, mem_engine):
        """Adding to a sector that already exceeds 30% should block."""
        guard = self._guard(mem_engine)
        # Hold 28% in Technology already (NVDA + META).  Adding 5% AAPL → 33%.
        positions = {
            "NVDA": {"shares": 100, "entry_price": 200, "current_price": 200},  # $20k
            "META": {"shares":  20, "entry_price": 400, "current_price": 400},  # $8k
        }
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(value=5_000),  # +5% Tech
            equity=100_000, positions=positions, daily_pnl_pct=0.0,
        )
        assert not result.passed
        assert result.checks.get("sector_exposure") is False
        assert "Technology" in result.reason

    def test_sector_exposure_passes_when_under_cap(self, mem_engine):
        """Same sector but under 30% cap should pass."""
        guard = self._guard(mem_engine)
        # Hold only 10% in Technology; adding 5% AAPL → 15%.
        positions = {
            "NVDA": {"shares": 50, "entry_price": 200, "current_price": 200},   # $10k
        }
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(value=5_000),
            equity=100_000, positions=positions, daily_pnl_pct=0.0,
        )
        assert result.passed
        assert result.checks.get("sector_exposure") is True

    def test_sector_exposure_only_sums_matching_sector(self, mem_engine):
        """A Healthcare position should not inflate Technology sector exposure."""
        guard = self._guard(mem_engine)
        positions = {
            "NVDA": {"shares": 50, "entry_price": 200, "current_price": 200},   # $10k Tech
            "JNJ":  {"shares": 60, "entry_price": 150, "current_price": 150},   # $9k Healthcare
        }
        # Adding 5% AAPL → Tech total 15% (well under 30%); Healthcare unaffected.
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=self._pos_size(value=5_000),
            equity=100_000, positions=positions, daily_pnl_pct=0.0,
        )
        assert result.passed

    def test_sector_check_skipped_for_unknown_symbol(self, mem_engine):
        """Symbols absent from _SECTOR_MAP pass the sector check (best-effort)."""
        guard = self._guard(mem_engine)
        ps = self._pos_size(value=5_000)
        ps.symbol = "ZZZZ"
        result = guard.check(
            symbol="ZZZZ", signal="BUY",
            position_size=ps,
            equity=100_000, positions={}, daily_pnl_pct=0.0,
        )
        assert result.passed
        assert result.checks.get("sector_exposure") is True


# ─────────────────────────────────────────────────────────────────────────────
# _SECTOR_MAP coverage of the active universe
# ─────────────────────────────────────────────────────────────────────────────

class TestSectorMapCoverage:
    """Regression guard for sector-map coverage.

    The 2026-05-12 expansion mapped the entire active universe to a sector.
    Future universe additions must extend `_SECTOR_MAP` too — otherwise the
    30%-per-sector cap silently passes those symbols.  This test makes the
    gap loud.  When a new symbol is added to `universe_assets` but not the
    map, this test names it.
    """

    def test_every_active_universe_symbol_is_mapped(self, mem_engine):
        from sqlalchemy import text
        from risk.portfolio_guard import _SECTOR_MAP
        rows = mem_engine.connect().execute(
            text("SELECT symbol FROM universe_assets WHERE active = 1")
        ).fetchall()
        active = [r[0] for r in rows]
        # When the in-memory DB is empty (typical for this fixture), the test
        # is a no-op — the production DB coverage check is what we care about.
        if not active:
            pytest.skip("no active universe rows in test DB — coverage check is a no-op")
        unmapped = sorted(s for s in active if s.upper() not in _SECTOR_MAP)
        assert not unmapped, (
            f"{len(unmapped)} active-universe symbols missing from _SECTOR_MAP: "
            f"{unmapped}. Add them to risk/portfolio_guard.py."
        )


# ─────────────────────────────────────────────────────────────────────────────
# CircuitBreaker
# ─────────────────────────────────────────────────────────────────────────────

class TestCircuitBreaker:

    def _cb(self):
        from risk.circuit_breaker import CircuitBreaker
        return CircuitBreaker()

    def test_initial_state_not_halted(self, mem_engine):
        halted, _ = self._cb().is_halted()
        assert halted is False

    def test_trigger_sets_halted(self, mem_engine):
        cb = self._cb()
        cb.trigger("daily loss limit")
        halted, reason = cb.is_halted()
        assert halted is True
        assert "daily loss limit" in reason

    def test_reset_clears_halt(self, mem_engine):
        cb = self._cb()
        cb.trigger("test")
        cb.reset()
        halted, _ = cb.is_halted()
        assert halted is False

    def test_auto_reset_after_timeout(self, mem_engine):
        """A TRIGGERED event older than reset_hours is auto-reset."""
        from data.database import CircuitBreakerLog

        cb = self._cb()
        # Insert a TRIGGERED event with triggered_at 25 hours ago
        old_time = _now() - timedelta(hours=25)
        with Session(mem_engine) as session:
            session.add(CircuitBreakerLog(
                event="TRIGGERED",
                reason="old trigger",
                triggered_at=old_time,
                reset_at=None,
                recorded_at=old_time,
            ))
            session.commit()

        halted, _ = cb.is_halted()
        assert halted is False   # auto-reset applied

    def test_status_returns_correct_fields(self, mem_engine):
        status = self._cb().get_status()
        assert "halted" in status
        assert "reason" in status
        assert "last_event" in status
        assert "triggered_at" in status


# ─────────────────────────────────────────────────────────────────────────────
# OrderManager
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderManager:

    def _signal_result(self, symbol="AAPL", signal="BUY"):
        from models.signal_gate import SignalResult
        return SignalResult(
            symbol=symbol,
            bar_timestamp=_now(),
            lstm_score=0.6,
            xgb_score=0.5,
            finbert_score=0.4,
            ensemble_score=0.55,
            signal=signal,
            passed_gate=True,
            gate_reason="passed",
        )

    def test_dry_run_decision(self, mem_engine):
        """dry_run=True always produces DRY_RUN decision, no IBKR call."""
        from risk.order_manager import OrderManager
        from risk.portfolio_guard import GuardResult

        ibkr_mock = MagicMock()
        mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=True)

        with patch("risk.order_manager.OrderManager._get_latest_close", return_value=200.0), \
             patch("risk.portfolio_guard.PortfolioGuard.check",
                   return_value=GuardResult(passed=True, reason="mocked pass", checks={})):
            decision = mgr.process(
                signal_result=self._signal_result(),
                equity=100_000,
                positions={},
            )

        assert decision.decision == "DRY_RUN"
        ibkr_mock.place_bracket_order.assert_not_called()

    def test_guard_rejection_logged(self, mem_engine):
        """PortfolioGuard failure → decision='REJECTED', persisted to DB."""
        from risk.order_manager import OrderManager
        from data.database import get_order_decisions

        mgr = OrderManager(dry_run=True)

        with patch("risk.order_manager.OrderManager._get_latest_close", return_value=200.0), \
             patch("risk.portfolio_guard.PortfolioGuard.check") as mock_check:
            from risk.portfolio_guard import GuardResult
            mock_check.return_value = GuardResult(
                passed=False, reason="test rejection", checks={}
            )
            decision = mgr.process(
                signal_result=self._signal_result(),
                equity=100_000,
                positions={},
                run_id="test-run",
            )

        assert decision.decision == "REJECTED"
        assert "test rejection" in decision.reject_reason

        # Persisted to DB
        df = get_order_decisions(limit=10)
        assert not df.empty
        assert "REJECTED" in df["decision"].values

    def test_approved_decision_fields_populated(self, mem_engine):
        """An approved dry-run decision has all key fields filled."""
        from risk.order_manager import OrderManager

        mgr = OrderManager(dry_run=True)

        with patch("risk.order_manager.OrderManager._get_latest_close", return_value=150.0):
            decision = mgr.process(
                signal_result=self._signal_result("MSFT"),
                equity=100_000,
                positions={},
            )

        assert decision.symbol == "MSFT"
        assert decision.signal == "BUY"
        assert decision.entry_price == 150.0
        assert decision.stop_price < decision.entry_price   # stop below entry for BUY
        assert decision.take_profit_price > decision.entry_price

    def test_simulation_mode_no_order_submitted(self, mem_engine):
        """SIMULATION mode + paper_orders_enabled=False → DRY_RUN even without dry_run flag."""
        from config.settings import config, TradingMode
        from risk.order_manager import OrderManager
        from risk.portfolio_guard import GuardResult

        original_mode    = config.trading.mode
        original_enabled = config.trading.paper_orders_enabled
        config.trading.mode               = TradingMode.SIMULATION
        config.trading.paper_orders_enabled = False

        try:
            ibkr_mock = MagicMock()
            mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=200.0), \
                 patch("risk.portfolio_guard.PortfolioGuard.check",
                       return_value=GuardResult(passed=True, reason="mocked pass", checks={})):
                decision = mgr.process(
                    signal_result=self._signal_result(),
                    equity=100_000,
                    positions={},
                )

            assert decision.decision == "DRY_RUN"
            ibkr_mock.place_bracket_order.assert_not_called()
        finally:
            config.trading.mode               = original_mode
            config.trading.paper_orders_enabled = original_enabled

    def test_sell_signal_closes_existing_long(self, mem_engine):
        """SELL + existing long position → CLOSED_LONG, market sell order placed."""
        from config.settings import config
        from risk.order_manager import OrderManager

        original_short   = config.trading.allow_short_selling
        original_enabled = config.trading.paper_orders_enabled
        config.trading.allow_short_selling  = False
        # Enable paper orders so the live-order path is reached (not dry_run override)
        config.trading.paper_orders_enabled = True

        try:
            ibkr_mock = MagicMock()
            mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

            positions = {"AAPL": {"shares": 25, "entry_price": 180.0, "current_price": 190.0}}

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=190.0), \
                 patch("risk.order_manager.OrderManager._submit_market_close", return_value=True) as mock_close:
                decision = mgr.process(
                    signal_result=self._signal_result("AAPL", "SELL"),
                    equity=100_000,
                    positions=positions,
                )

            assert decision.decision == "CLOSED_LONG"
            assert decision.symbol == "AAPL"
            assert decision.shares == 25
            assert decision.entry_price == 190.0
            mock_close.assert_called_once_with("AAPL", 25)
            # Should never touch the bracket order path
            ibkr_mock.place_bracket_order.assert_not_called()
        finally:
            config.trading.allow_short_selling  = original_short
            config.trading.paper_orders_enabled = original_enabled

    def test_close_long_cancels_orphan_bracket_children(self, mem_engine):
        """SELL + held long must cancel any open SELL LMT (TP) and SELL STP
        legs before flattening.  Without this, the orphan stops remain live
        and a later trigger opens an unintended SHORT against zero shares —
        exactly the SLV bug from 2026-04-29."""
        import asyncio
        from config.settings import config
        from risk.order_manager import OrderManager

        original_short   = config.trading.allow_short_selling
        original_enabled = config.trading.paper_orders_enabled
        config.trading.allow_short_selling  = False
        config.trading.paper_orders_enabled = True

        try:
            loop = asyncio.new_event_loop()

            async def _orders():
                return [
                    {"order_id": 100, "symbol": "AAPL", "action": "SELL",
                     "order_type": "LMT", "limit_price": 220.0, "stop_price": None},
                    {"order_id": 101, "symbol": "AAPL", "action": "SELL",
                     "order_type": "STP", "limit_price": None, "stop_price": 170.0},
                    # Unrelated orders that must NOT be cancelled
                    {"order_id": 200, "symbol": "MSFT", "action": "SELL",
                     "order_type": "STP", "limit_price": None, "stop_price": 380.0},
                    {"order_id": 201, "symbol": "AAPL", "action": "BUY",
                     "order_type": "LMT", "limit_price": 180.0, "stop_price": None},
                ]

            async def _cancel(order_id):
                return True

            async def _market(*_a, **_k):
                return True

            ibkr_mock = MagicMock()
            ibkr_mock.get_open_orders = MagicMock(side_effect=lambda: _orders())
            ibkr_mock.cancel_order    = MagicMock(side_effect=lambda oid: _cancel(oid))
            ibkr_mock.place_market_order = MagicMock(side_effect=lambda *a, **k: _market())

            mgr = OrderManager(
                ibkr_connection=ibkr_mock, dry_run=False, event_loop=loop,
            )
            positions = {"AAPL": {"shares": 25, "entry_price": 180.0, "current_price": 190.0}}

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=190.0):
                decision = mgr.process(
                    signal_result=self._signal_result("AAPL", "SELL"),
                    equity=100_000,
                    positions=positions,
                )

            assert decision.decision == "CLOSED_LONG"
            cancelled_ids = [c.args[0] for c in ibkr_mock.cancel_order.call_args_list]
            assert 100 in cancelled_ids   # SELL LMT (TP) cancelled
            assert 101 in cancelled_ids   # SELL STP cancelled
            assert 200 not in cancelled_ids  # different symbol untouched
            assert 201 not in cancelled_ids  # BUY LMT untouched
            ibkr_mock.place_market_order.assert_called_once()
            loop.close()
        finally:
            config.trading.allow_short_selling  = original_short
            config.trading.paper_orders_enabled = original_enabled

    def test_cancel_bracket_children_cancels_trail_orders(self, mem_engine):
        """Regression guard against the orphan-TRAIL bug observed in production
        on 2026-05-20 (ASTS TRAIL id=173 survived a same-run trail-conversion +
        signal-flip close; manually cleaned up 29 minutes later by the user).
        The orphan, if not caught manually, would have fired as a short on the
        next price trigger, defeating the long-only gate.

        The fix is a one-character widening of the cancel-filter tuple in
        ``OrderManager._cancel_bracket_children`` from ``("LMT", "STP", "STP LMT")``
        to ``("LMT", "STP", "STP LMT", "TRAIL")``.  This test pins that filter
        membership by including a live TRAIL alongside the typical LMT/STP
        pair and asserting all three are cancelled (and only the matching
        ones — unrelated orders for other symbols / BUY actions stay live).
        """
        import asyncio
        from config.settings import config
        from risk.order_manager import OrderManager

        original_short   = config.trading.allow_short_selling
        original_enabled = config.trading.paper_orders_enabled
        config.trading.allow_short_selling  = False
        config.trading.paper_orders_enabled = True

        try:
            loop = asyncio.new_event_loop()

            async def _orders():
                return [
                    # The three bracket-child legs for the symbol being closed —
                    # all must be cancelled before the market sell.  TRAIL is
                    # the key addition this test pins.
                    {"order_id": 100, "symbol": "ASTS", "action": "SELL",
                     "order_type": "LMT", "limit_price": 95.0, "stop_price": None},
                    {"order_id": 101, "symbol": "ASTS", "action": "SELL",
                     "order_type": "STP", "limit_price": None, "stop_price": 78.0},
                    {"order_id": 102, "symbol": "ASTS", "action": "SELL",
                     "order_type": "TRAIL", "limit_price": None, "stop_price": 6.0},
                    # Unrelated orders that must NOT be cancelled
                    {"order_id": 200, "symbol": "MSFT", "action": "SELL",
                     "order_type": "TRAIL", "limit_price": None, "stop_price": 8.0},
                    {"order_id": 201, "symbol": "ASTS", "action": "BUY",
                     "order_type": "LMT", "limit_price": 80.0, "stop_price": None},
                ]

            async def _cancel(order_id):
                return True

            async def _market(*_a, **_k):
                return True

            ibkr_mock = MagicMock()
            ibkr_mock.get_open_orders = MagicMock(side_effect=lambda: _orders())
            ibkr_mock.cancel_order    = MagicMock(side_effect=lambda oid: _cancel(oid))
            ibkr_mock.place_market_order = MagicMock(side_effect=lambda *a, **k: _market())

            mgr = OrderManager(
                ibkr_connection=ibkr_mock, dry_run=False, event_loop=loop,
            )
            positions = {"ASTS": {"shares": 100, "entry_price": 84.0, "current_price": 90.0}}

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=90.0):
                decision = mgr.process(
                    signal_result=self._signal_result("ASTS", "SELL"),
                    equity=100_000,
                    positions=positions,
                )

            assert decision.decision == "CLOSED_LONG"
            cancelled_ids = [c.args[0] for c in ibkr_mock.cancel_order.call_args_list]
            assert 100 in cancelled_ids   # SELL LMT (TP) cancelled
            assert 101 in cancelled_ids   # SELL STP cancelled
            assert 102 in cancelled_ids   # SELL TRAIL cancelled — the bug fix
            assert 200 not in cancelled_ids  # different symbol untouched
            assert 201 not in cancelled_ids  # BUY LMT untouched
            ibkr_mock.place_market_order.assert_called_once()
            loop.close()
        finally:
            config.trading.allow_short_selling  = original_short
            config.trading.paper_orders_enabled = original_enabled

    def test_sell_signal_ignored_when_no_long_held(self, mem_engine):
        """SELL + no existing position → REJECTED_NO_POSITION, no order placed."""
        from config.settings import config
        from risk.order_manager import OrderManager

        original_short = config.trading.allow_short_selling
        config.trading.allow_short_selling = False

        try:
            ibkr_mock = MagicMock()
            mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=190.0):
                decision = mgr.process(
                    signal_result=self._signal_result("AAPL", "SELL"),
                    equity=100_000,
                    positions={},   # no positions
                )

            assert decision.decision == "REJECTED_NO_POSITION"
            assert "short selling not enabled" in decision.reject_reason
            ibkr_mock.place_bracket_order.assert_not_called()
            ibkr_mock.place_market_order.assert_not_called()
        finally:
            config.trading.allow_short_selling = original_short

    def test_zero_shares_rejected_too_small(self, mem_engine):
        """PositionSizer returns shares=0 → REJECTED_TOO_SMALL, no guard/IBKR call."""
        from risk.order_manager import OrderManager
        from risk.position_sizer import PositionSize

        ibkr_mock = MagicMock()
        mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

        zero_size = PositionSize(
            symbol="AAPL", signal="BUY", shares=0,
            entry_price=200.0, stop_price=196.0, take_profit_price=206.0,
            position_value=150.0,  # below entry_price → sizer produced 0 shares
            position_pct=0.0015,
            kelly_fraction_used=0.01, method="fixed",
        )

        with patch("risk.order_manager.OrderManager._get_latest_close", return_value=200.0), \
             patch("risk.position_sizer.PositionSizer.calculate", return_value=zero_size), \
             patch("risk.portfolio_guard.PortfolioGuard.check") as mock_check:
            decision = mgr.process(
                signal_result=self._signal_result("AAPL", "BUY"),
                equity=100_000,
                positions={},
            )

        assert decision.decision == "REJECTED_TOO_SMALL"
        assert decision.shares == 0
        assert "below 1 share" in decision.reject_reason
        # Guard must not be called — we reject before it
        mock_check.assert_not_called()
        # IBKR must not be called
        ibkr_mock.place_bracket_order.assert_not_called()

    def test_zero_entry_price_rejected_too_small(self, mem_engine):
        """entry_price=0 (no bars cached) → sizer returns 0 shares → REJECTED_TOO_SMALL."""
        from risk.order_manager import OrderManager

        ibkr_mock = MagicMock()
        mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

        # _get_latest_close returns 0 when no bars exist; sizer will produce 0 shares
        with patch("risk.order_manager.OrderManager._get_latest_close", return_value=0.0):
            decision = mgr.process(
                signal_result=self._signal_result("UNKNOWN", "BUY"),
                equity=100_000,
                positions={},
            )

        assert decision.decision == "REJECTED_TOO_SMALL"
        assert decision.shares == 0
        ibkr_mock.place_bracket_order.assert_not_called()

    def test_allow_short_selling_false_never_opens_short(self, mem_engine):
        """With allow_short_selling=False, no SELL signal ever opens a short position."""
        from config.settings import config
        from risk.order_manager import OrderManager

        original_short = config.trading.allow_short_selling
        config.trading.allow_short_selling = False

        try:
            ibkr_mock = MagicMock()
            mgr = OrderManager(ibkr_connection=ibkr_mock, dry_run=False)

            with patch("risk.order_manager.OrderManager._get_latest_close", return_value=200.0):
                # Scenario 1: SELL with no position
                d1 = mgr.process(
                    signal_result=self._signal_result("MSFT", "SELL"),
                    equity=100_000,
                    positions={},
                )
                assert d1.decision == "REJECTED_NO_POSITION"

                # Scenario 2: SELL with an existing long (closes it, doesn't short)
                with patch("risk.order_manager.OrderManager._submit_market_close", return_value=True):
                    d2 = mgr.process(
                        signal_result=self._signal_result("MSFT", "SELL"),
                        equity=100_000,
                        positions={"MSFT": {"shares": 10, "entry_price": 195.0, "current_price": 200.0}},
                    )
                assert d2.decision == "CLOSED_LONG"

            # Bracket order must never have been called for a SELL signal
            ibkr_mock.place_bracket_order.assert_not_called()
        finally:
            config.trading.allow_short_selling = original_short
