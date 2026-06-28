"""Tests for scripts/rebalance.py wiring (pure assembly + mocked IBKR fetch).

The heavy logic is in portfolio.allocation (tested separately); these pin the
row→Target conversion, the plan assembly, the formatter, and the IBKR fetch
adapter (with a mocked async connection — no Gateway needed)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from portfolio.allocation import CORE, SAT_BIGBET
from scripts.rebalance import (
    _fetch_state, build_plan, format_plan, targets_from_rows,
)


@pytest.fixture()
def mem_engine(monkeypatch):
    from data.database import Base
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def test_targets_from_rows():
    rows = [{"ticker": "VLUE", "sleeve": CORE, "target_weight": 0.5, "label": "x"},
            {"ticker": "BB", "sleeve": SAT_BIGBET, "target_weight": 0.025, "label": None}]
    ts = targets_from_rows(rows)
    assert ts[0].ticker == "VLUE" and ts[0].sleeve == CORE and ts[0].weight == 0.5
    assert ts[1].sleeve == SAT_BIGBET and ts[1].label == ""        # None → ""


def test_build_plan_on_target_no_trades():
    rows = [{"ticker": "A", "sleeve": CORE, "target_weight": 0.5},
            {"ticker": "B", "sleeve": CORE, "target_weight": 0.5}]
    plan = build_plan(rows, {"A": 50, "B": 100}, {"A": 100, "B": 50},
                      10_000, 0, band=0.05, cash_buffer=0.0)
    assert plan.trades == []


def test_build_plan_drift_proposes_trades():
    rows = [{"ticker": "A", "sleeve": CORE, "target_weight": 0.5},
            {"ticker": "B", "sleeve": CORE, "target_weight": 0.5}]
    plan = build_plan(rows, {"A": 70, "B": 60}, {"A": 100, "B": 50},
                      10_000, 0, band=0.05, cash_buffer=0.0)
    assert {p.action for p in plan.trades} == {"SELL", "BUY"}


def test_format_plan_smoke():
    rows = [{"ticker": "A", "sleeve": CORE, "target_weight": 1.0}]
    plan = build_plan(rows, {}, {"A": 100}, 10_000, 10_000, band=0.05, cash_buffer=0.0)
    s = format_plan(plan)
    assert "DRY-RUN" in s and "Proposed trades" in s and "no orders submitted" in s


def test_fetch_state_with_mocked_connection(mem_engine):
    from data.database import replace_target_sleeves
    replace_target_sleeves([{"ticker": "A", "sleeve": CORE, "target_weight": 1.0}], {CORE})

    summary = SimpleNamespace(net_liquidation=10_000.0, total_cash=500.0)

    class FakeConn:
        async def get_account_summary(self):
            return summary

        async def get_positions(self):
            return [{"symbol": "A", "quantity": 50}, {"symbol": "ZZZ", "quantity": 3}]

        async def get_last_price(self, t):
            return {"A": 100.0, "ZZZ": 20.0}.get(t)

    rows, holdings, prices, nlv, cash = asyncio.run(_fetch_state(FakeConn()))
    assert holdings == {"A": 50, "ZZZ": 3}
    assert prices == {"A": 100.0, "ZZZ": 20.0}
    assert nlv == 10_000.0 and cash == 500.0
    assert any(r["ticker"] == "A" for r in rows)
