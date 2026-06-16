"""Tests for the symbol-union helpers in scripts/refresh_recent_bars.py.

Focus: `_recently_exited_symbols`, which keeps just-exited live positions in the
EOD refresh union so post-exit bars keep flowing (the SNOW gap fix).  Bracket
exits reconcile into trade_log with no order_decisions row, so this trade_log
path is the only thing that can catch them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture()
def mem_engine(monkeypatch):
    from data.database import Base
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_trade(engine, *, symbol, exit_ts, source="live", entry_ts=None):
    from data.database import TradeLog
    with Session(engine) as s:
        s.add(TradeLog(
            source=source, run_id="r", fold_index=0, symbol=symbol, signal="BUY",
            entry_ts=entry_ts or (exit_ts - timedelta(days=3)), entry_px=100.0,
            exit_ts=exit_ts, exit_px=110.0, exit_reason="trailing",
            shares=10.0, pnl=100.0, pnl_pct=0.1, costs_charged=0.0,
            recorded_at=exit_ts,
        ))
        s.commit()


def test_recent_live_exit_is_included(mem_engine):
    from scripts.refresh_recent_bars import _recently_exited_symbols
    _add_trade(mem_engine, symbol="SNOW", exit_ts=_now() - timedelta(days=3))
    assert "SNOW" in _recently_exited_symbols(days=14)


def test_old_exit_is_excluded(mem_engine):
    from scripts.refresh_recent_bars import _recently_exited_symbols
    _add_trade(mem_engine, symbol="OLD", exit_ts=_now() - timedelta(days=40))
    assert "OLD" not in _recently_exited_symbols(days=14)


def test_walk_forward_exit_is_ignored(mem_engine):
    from scripts.refresh_recent_bars import _recently_exited_symbols
    # Recent exit but source='walk_forward' — not a real held position.
    _add_trade(mem_engine, symbol="WFONLY", exit_ts=_now() - timedelta(days=2),
               source="walk_forward")
    assert "WFONLY" not in _recently_exited_symbols(days=14)


def test_empty_trade_log_returns_empty_set(mem_engine):
    from scripts.refresh_recent_bars import _recently_exited_symbols
    assert _recently_exited_symbols(days=14) == set()


# ── Unfilled-entry sweep (_cancel_unfilled_entries) ─────────────────────────

class _FakeIBKR:
    """Minimal async stand-in for IBKRConnection used by the EOD cancel sweep."""

    def __init__(self, *, positions, orders, connect_ok=True):
        self._positions = positions
        self._orders = orders
        self._connect_ok = connect_ok
        self.cancelled: list[int] = []

    async def connect(self):
        return self._connect_ok

    async def get_positions(self):
        return self._positions

    async def get_open_orders(self):
        return self._orders

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    async def disconnect(self):
        return None


def _patch_ibkr(monkeypatch, fake):
    monkeypatch.setattr("execution.ibkr_connection.IBKRConnection", lambda *a, **k: fake)


def _cancelled_decisions(engine):
    from scripts.refresh_recent_bars import _cancel_unfilled_entries  # noqa: F401
    from data.database import get_order_decisions
    df = get_order_decisions(limit=100)
    if df.empty:
        return df
    return df[df["decision"] == "CANCELLED_UNFILLED"]


def test_sweep_skips_held_symbol_protective_legs(mem_engine, monkeypatch):
    """A working order on a HELD symbol is a protective leg — never cancelled.
    A working order on a NOT-held symbol is an unfilled entry — cancelled."""
    from scripts.refresh_recent_bars import _cancel_unfilled_entries
    fake = _FakeIBKR(
        positions=[{"symbol": "GE", "quantity": 100}],
        orders=[
            # GE protective stop (held → must be left alone)
            {"symbol": "GE", "order_id": 1, "action": "SELL", "order_type": "STP",
             "limit_price": None, "quantity": 100, "remaining": 100},
            # CVX unfilled entry (not held → cancel)
            {"symbol": "CVX", "order_id": 2, "action": "BUY", "order_type": "LMT",
             "limit_price": 150.0, "quantity": 50, "remaining": 50},
        ],
    )
    _patch_ibkr(monkeypatch, fake)

    n = _cancel_unfilled_entries()

    assert n == 1
    assert fake.cancelled == [2]            # only CVX, never the GE stop
    df = _cancelled_decisions(mem_engine)
    assert list(df["symbol"]) == ["CVX"]
    assert df.iloc[0]["entry_price"] == 150.0


def test_sweep_tears_down_whole_bracket_on_unheld_symbol(mem_engine, monkeypatch):
    """All legs of an unfilled bracket share the symbol → all cancelled, but
    only ONE audit row, priced off the BUY LMT entry leg."""
    from scripts.refresh_recent_bars import _cancel_unfilled_entries
    fake = _FakeIBKR(
        positions=[],
        orders=[
            {"symbol": "CVX", "order_id": 10, "action": "BUY", "order_type": "LMT",
             "limit_price": 150.0, "quantity": 50, "remaining": 50},
            {"symbol": "CVX", "order_id": 11, "action": "SELL", "order_type": "STP",
             "limit_price": None, "quantity": 50, "remaining": 50},
            {"symbol": "CVX", "order_id": 12, "action": "SELL", "order_type": "LMT",
             "limit_price": 165.0, "quantity": 50, "remaining": 50},
        ],
    )
    _patch_ibkr(monkeypatch, fake)

    n = _cancel_unfilled_entries()

    assert n == 1
    assert sorted(fake.cancelled) == [10, 11, 12]
    df = _cancelled_decisions(mem_engine)
    assert len(df) == 1
    assert df.iloc[0]["entry_price"] == 150.0   # the BUY LMT, not the SELL LMT TP


def test_sweep_noop_when_gateway_unreachable(mem_engine, monkeypatch):
    from scripts.refresh_recent_bars import _cancel_unfilled_entries
    fake = _FakeIBKR(positions=[], orders=[], connect_ok=False)
    _patch_ibkr(monkeypatch, fake)

    assert _cancel_unfilled_entries() == 0
    assert fake.cancelled == []
    assert _cancelled_decisions(mem_engine).empty


def test_sweep_noop_when_no_unfilled_entries(mem_engine, monkeypatch):
    from scripts.refresh_recent_bars import _cancel_unfilled_entries
    fake = _FakeIBKR(
        positions=[{"symbol": "GE", "quantity": 100}],
        orders=[{"symbol": "GE", "order_id": 1, "action": "SELL", "order_type": "STP",
                 "limit_price": None, "quantity": 100, "remaining": 100}],
    )
    _patch_ibkr(monkeypatch, fake)

    assert _cancel_unfilled_entries() == 0
    assert fake.cancelled == []


def test_live_orders_active_gate(monkeypatch):
    from scripts.refresh_recent_bars import _live_orders_active
    from config.settings import TradingMode, config

    monkeypatch.setattr(config.trading, "mode", TradingMode.SIMULATION)
    monkeypatch.setattr(config.trading, "paper_orders_enabled", False)
    assert _live_orders_active() is False

    monkeypatch.setattr(config.trading, "paper_orders_enabled", True)
    assert _live_orders_active() is True

    monkeypatch.setattr(config.trading, "mode", TradingMode.LIVE)
    monkeypatch.setattr(config.trading, "paper_orders_enabled", False)
    assert _live_orders_active() is True
