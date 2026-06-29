"""Tests for the cost-basis holdings model (compute_holdings_from_fills)."""
from __future__ import annotations

from datetime import datetime

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


def _seed(fills: list[dict]):
    from data.database import FillLog, get_engine
    with Session(get_engine()) as s:
        for i, f in enumerate(fills):
            s.add(FillLog(exec_id=str(i), recorded_at=datetime(2026, 1, 1), **f))
        s.commit()


def test_single_buy(mem_engine):
    from data.database import compute_holdings_from_fills
    _seed([dict(symbol="VLUE", side="BUY", shares=10, price=100, commission=1,
                exec_time=datetime(2026, 1, 1))])
    h = compute_holdings_from_fills()["VLUE"]
    assert h["shares"] == 10
    assert h["cost_basis"] == pytest.approx(1001)         # 10*100 + 1 commission
    assert h["avg_cost"] == pytest.approx(100.1)


def test_average_cost_over_two_buys(mem_engine):
    from data.database import compute_holdings_from_fills
    _seed([dict(symbol="QUAL", side="BUY", shares=10, price=100, commission=0, exec_time=datetime(2026, 1, 1)),
           dict(symbol="QUAL", side="BUY", shares=10, price=120, commission=0, exec_time=datetime(2026, 1, 2))])
    h = compute_holdings_from_fills()["QUAL"]
    assert h["shares"] == 20
    assert h["avg_cost"] == pytest.approx(110.0)          # (1000 + 1200) / 20


def test_partial_sell_realises_pnl_keeps_avg_cost(mem_engine):
    from data.database import compute_holdings_from_fills
    _seed([dict(symbol="IEF", side="BUY", shares=10, price=100, commission=0, exec_time=datetime(2026, 1, 1)),
           dict(symbol="IEF", side="SELL", shares=4, price=130, commission=0, exec_time=datetime(2026, 1, 2))])
    h = compute_holdings_from_fills()["IEF"]
    assert h["shares"] == 6
    assert h["avg_cost"] == pytest.approx(100.0)          # average cost unchanged by the sale
    assert h["realized_pnl"] == pytest.approx(120.0)      # 4 * (130 - 100)


def test_full_close_leaves_zero_shares_and_realised(mem_engine):
    from data.database import compute_holdings_from_fills
    _seed([dict(symbol="GLD", side="BUY", shares=10, price=100, commission=0, exec_time=datetime(2026, 1, 1)),
           dict(symbol="GLD", side="SELL", shares=10, price=110, commission=0, exec_time=datetime(2026, 1, 2))])
    h = compute_holdings_from_fills()["GLD"]
    assert h["shares"] == 0
    assert h["realized_pnl"] == pytest.approx(100.0)      # 10 * (110 - 100)
    assert h["avg_cost"] == 0.0                           # no open position
