"""Tests for the execution wrapper (portfolio/rebalancer.py) + rebalance_log.

The order loop is driven with a mocked async connection — no IBKR/Gateway."""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from portfolio.allocation import CORE, RebalancePlan, Target, TradeProposal, compute_plan
from portfolio.rebalancer import marketable_limit, submit_plan


@pytest.fixture()
def mem_engine(monkeypatch):
    from data.database import Base
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


class _FakeConn:
    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = set(fail_on or [])

    async def place_limit_order(self, symbol, action, quantity, limit_price):
        self.calls.append((symbol, action, quantity, limit_price))
        if symbol in self._fail_on:
            raise RuntimeError("boom")
        return SimpleNamespace(order_id=len(self.calls), status="Submitted")


def _drift_plan():
    # A overweight (→ SELL), B underweight (→ BUY)
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5)]
    return compute_plan(targets, {"A": 70, "B": 60}, {"A": 100, "B": 50},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)


def test_marketable_limit_bounds_slippage():
    assert marketable_limit("BUY", 100.0, 0.005) == 100.50     # pay up 0.5%
    assert marketable_limit("SELL", 100.0, 0.005) == 99.50     # accept down 0.5%
    assert marketable_limit("buy", 33.333, 0.01) == round(33.333 * 1.01, 2)   # tick-rounded


def test_submit_plan_places_marketable_limits():
    conn = _FakeConn()
    results = asyncio.run(submit_plan(conn, _drift_plan(), slippage_cap=0.005, share_precision=4))
    by = {r.ticker: r for r in results}
    assert by["A"].action == "SELL" and by["A"].limit_price == 99.50 and by["A"].shares == 20
    assert by["B"].action == "BUY" and by["B"].limit_price == 50.25 and by["B"].shares == 40
    assert all(r.ok for r in results)
    assert ("A", "SELL", 20.0, 99.50) in conn.calls


def test_submit_plan_skips_zero_qty_and_no_price():
    p_small = TradeProposal("A", CORE, 0, 0, 0, 0, 0, "BUY", 30, 0.3, 100, "x")   # 0.3 sh
    p_noprice = TradeProposal("B", CORE, 0, 0, 0, 0, 0, "BUY", 5, 5, 0, "x")      # price 0
    plan = RebalancePlan(datetime.utcnow(), 10_000, 0, 10_000, 0, 0, 0.05,
                         [p_small, p_noprice], [])
    conn = _FakeConn()
    results = asyncio.run(submit_plan(conn, plan, share_precision=0))
    assert all(r.status.startswith("SKIPPED") for r in results)
    assert conn.calls == []                                   # nothing submitted


def test_submit_plan_records_failures_without_aborting():
    conn = _FakeConn(fail_on={"A"})
    results = asyncio.run(submit_plan(conn, _drift_plan()))
    by = {r.ticker: r for r in results}
    assert by["A"].status.startswith("FAILED") and not by["A"].ok
    assert by["B"].ok                                         # B submitted despite A failing


def test_log_rebalance(mem_engine):
    from sqlalchemy.orm import Session
    from data.database import RebalanceLog, get_engine, log_rebalance
    log_rebalance({"run_id": "x", "run_at": datetime.utcnow(), "mode": "live",
                   "nlv": 10_000, "n_proposed": 2, "n_submitted": 2, "n_failed": 0,
                   "turnover_pct": 40.0, "notes": None})
    with Session(get_engine()) as s:
        assert s.query(RebalanceLog).count() == 1
