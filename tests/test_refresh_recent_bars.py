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
