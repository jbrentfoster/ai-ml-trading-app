"""Unit tests for the pure rebalance engine (portfolio/allocation.py).

No IBKR/DB/network — feed target/holdings/price dicts, assert the plan.
"""
from __future__ import annotations

import pytest

from portfolio.allocation import (
    CORE, SAT_QV, SAT_BIGBET, Target, compute_plan,
)


def _prop(plan, ticker):
    return next(p for p in plan.proposals if p.ticker == ticker)


# ── Managed rebalance: band gate, sizing, cash ────────────────────────────────

def test_on_target_all_hold():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5)]
    plan = compute_plan(targets, {"A": 50, "B": 100}, {"A": 100, "B": 50},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    assert all(p.action == "HOLD" for p in plan.proposals)
    assert plan.trades == []
    assert plan.turnover_pct == 0.0


def test_drift_beyond_band_trims_and_tops_up():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5)]
    # A is 70% (target 50%), B is 30% → both 20pp off
    plan = compute_plan(targets, {"A": 70, "B": 60}, {"A": 100, "B": 50},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    a, b = _prop(plan, "A"), _prop(plan, "B")
    assert a.action == "SELL" and a.dollars == pytest.approx(-2000) and a.shares == pytest.approx(-20)
    assert b.action == "BUY" and b.dollars == pytest.approx(2000) and b.shares == pytest.approx(40)
    assert plan.turnover_pct == pytest.approx(40.0)


def test_drift_within_band_holds():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5)]
    # A 52% / B 48% → 2pp off, under the 5pp band
    plan = compute_plan(targets, {"A": 52, "B": 96}, {"A": 100, "B": 50},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    assert all(p.action == "HOLD" for p in plan.proposals)


def test_idle_cash_is_deployed_to_underweight():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5)]
    # each holds $4k, $2k cash idle → both underweight by 10pp → buy with cash
    plan = compute_plan(targets, {"A": 40, "B": 80}, {"A": 100, "B": 50},
                        nlv=10_000, cash=2000, band=0.05, cash_buffer=0.0)
    a, b = _prop(plan, "A"), _prop(plan, "B")
    assert a.action == "BUY" and b.action == "BUY"
    assert (a.dollars + b.dollars) == pytest.approx(2000)   # exactly the idle cash


def test_cash_buffer_held_back():
    targets = [Target("A", CORE, 1.0)]
    plan = compute_plan(targets, {}, {"A": 100}, nlv=10_000, cash=10_000,
                        band=0.05, cash_buffer=0.01)
    a = _prop(plan, "A")
    assert a.action == "BUY"
    assert a.dollars == pytest.approx(9900)        # 1% ($100) kept as cash
    assert a.shares == pytest.approx(99)


def test_managed_weights_normalized_over_managed_book():
    # weights sum to 0.95 (a 5% big-bet bucket sits outside) → normalize to fill
    targets = [Target("A", CORE, 0.80), Target("B", SAT_QV, 0.15)]
    plan = compute_plan(targets, {}, {"A": 100, "B": 100}, nlv=10_000, cash=10_000,
                        band=0.05, cash_buffer=0.0)
    a, b = _prop(plan, "A"), _prop(plan, "B")
    assert a.target_value == pytest.approx(10_000 * 0.80 / 0.95)
    assert b.target_value == pytest.approx(10_000 * 0.15 / 0.95)
    assert (a.target_value + b.target_value) == pytest.approx(10_000)


def test_fractional_shares():
    targets = [Target("A", CORE, 1.0)]
    plan = compute_plan(targets, {}, {"A": 333.0}, nlv=10_000, cash=10_000,
                        band=0.05, cash_buffer=0.0)
    assert _prop(plan, "A").shares == pytest.approx(10_000 / 333.0)


def test_missing_price_holds_with_note():
    targets = [Target("A", CORE, 1.0)]
    plan = compute_plan(targets, {"A": 10}, {}, nlv=10_000, cash=0)
    a = _prop(plan, "A")
    assert a.action == "HOLD" and "no price" in a.reason
    assert any("no price" in n for n in plan.notes)


# ── Big-bets: drift-exempt ────────────────────────────────────────────────────

def test_bigbet_winner_is_never_trimmed():
    """A big-bet that 40x'd to 30% of NLV is left alone — not trimmed to its cap."""
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5),
               Target("BB", SAT_BIGBET, 0.025)]
    # BB = $3000 (30% of NLV); A/B on target within the managed book
    plan = compute_plan(targets, {"A": 35, "B": 70, "BB": 3},
                        {"A": 100, "B": 50, "BB": 1000},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    bb = _prop(plan, "BB")
    assert bb.action == "HOLD" and bb.dollars == 0.0
    assert bb.current_wt == pytest.approx(0.30)        # 30% of NLV, untouched
    assert plan.bigbet_value == pytest.approx(3000)
    # managed book rebalanced within NLV − big-bet
    assert plan.managed_nlv == pytest.approx(7000)
    assert _prop(plan, "A").action == "HOLD"


def test_bigbet_loser_is_not_topped_up():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5),
               Target("BB", SAT_BIGBET, 0.025)]
    # BB down to $200 (2% of NLV, below its 2.5% cap) — must NOT be topped up
    plan = compute_plan(targets, {"A": 49, "B": 98, "BB": 2},
                        {"A": 100, "B": 50, "BB": 100},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    bb = _prop(plan, "BB")
    assert bb.action == "HOLD" and bb.dollars == 0.0


def test_bigbet_unheld_target_initiates_entry_and_earmarks_capital():
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5),
               Target("BB", SAT_BIGBET, 0.025)]
    # BB not held; $500 cash available to fund the entry
    plan = compute_plan(targets, {"A": 47.5, "B": 95}, {"A": 100, "B": 50, "BB": 100},
                        nlv=10_000, cash=500, band=0.05, cash_buffer=0.0)
    bb = _prop(plan, "BB")
    assert bb.action == "BUY"
    assert bb.dollars == pytest.approx(250)            # 2.5% of NLV entry cap
    assert bb.shares == pytest.approx(2.5)
    assert plan.bigbet_pending == pytest.approx(250)
    # managed book is computed on NLV minus the earmarked entry capital
    assert plan.managed_nlv == pytest.approx(9750)


def test_untracked_holding_flagged_for_sell():
    targets = [Target("A", CORE, 1.0)]
    plan = compute_plan(targets, {"A": 95, "ZZZ": 10}, {"A": 100, "ZZZ": 50},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    z = _prop(plan, "ZZZ")
    assert z.sleeve == "untracked" and z.action == "SELL"
    assert z.dollars == pytest.approx(-500) and z.shares == pytest.approx(-10)
    assert any("untracked" in n for n in plan.notes)


def test_bigbet_balloon_does_not_false_trigger_managed_rebalance():
    """When a big-bet balloons, the managed sleeves stay balanced *among
    themselves* — they are not dragged 'underweight vs NLV'."""
    targets = [Target("A", CORE, 0.5), Target("B", CORE, 0.5),
               Target("BB", SAT_BIGBET, 0.025)]
    # BB is 50% of NLV; A/B split the other 50% evenly (on target within managed)
    plan = compute_plan(targets, {"A": 25, "B": 50, "BB": 5},
                        {"A": 100, "B": 50, "BB": 1000},
                        nlv=10_000, cash=0, band=0.05, cash_buffer=0.0)
    assert _prop(plan, "A").action == "HOLD"
    assert _prop(plan, "B").action == "HOLD"
    assert _prop(plan, "BB").action == "HOLD"
    assert plan.trades == []
