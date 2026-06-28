"""Tests for the target_allocation table helpers + set_targets cap validation."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine


@pytest.fixture()
def mem_engine(monkeypatch):
    from data.database import Base
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def test_replace_and_get_uppercases_and_activates(mem_engine):
    from data.database import replace_target_sleeves, get_target_allocation
    from portfolio.allocation import CORE
    n = replace_target_sleeves(
        [{"ticker": "vlue", "sleeve": CORE, "target_weight": 0.5},
         {"ticker": "ief", "sleeve": CORE, "target_weight": 0.5, "label": "bonds"}], {CORE})
    assert n == 2
    rows = get_target_allocation(active_only=True)
    assert {r["ticker"] for r in rows} == {"VLUE", "IEF"}     # upper-cased
    assert all(r["active"] for r in rows)


def test_replace_sleeve_preserves_history_and_other_sleeves(mem_engine):
    from data.database import replace_target_sleeves, get_target_allocation
    from portfolio.allocation import CORE, SAT_QV
    replace_target_sleeves([{"ticker": "VLUE", "sleeve": CORE, "target_weight": 0.8}], {CORE})
    replace_target_sleeves([{"ticker": "AAPL", "sleeve": SAT_QV, "target_weight": 0.15}], {SAT_QV})
    # re-screen the quality-value sleeve → AAPL out, KO in; core untouched
    replace_target_sleeves([{"ticker": "KO", "sleeve": SAT_QV, "target_weight": 0.15}], {SAT_QV})

    active = {r["ticker"] for r in get_target_allocation(active_only=True)}
    assert active == {"VLUE", "KO"}                          # core kept, qv replaced
    allrows = get_target_allocation(active_only=False)
    assert any(r["ticker"] == "AAPL" and not r["active"] for r in allrows)   # history kept


def test_get_filters_by_sleeve(mem_engine):
    from data.database import replace_target_sleeves, get_target_allocation
    from portfolio.allocation import CORE, SAT_BIGBET
    replace_target_sleeves([{"ticker": "VLUE", "sleeve": CORE, "target_weight": 0.8}], {CORE})
    replace_target_sleeves([{"ticker": "ANTH", "sleeve": SAT_BIGBET, "target_weight": 0.025}], {SAT_BIGBET})
    bb = get_target_allocation(sleeve=SAT_BIGBET)
    assert len(bb) == 1 and bb[0]["ticker"] == "ANTH"


def test_empty_spec_clears_a_sleeve(mem_engine):
    from data.database import replace_target_sleeves, get_target_allocation
    from portfolio.allocation import SAT_QV
    replace_target_sleeves([{"ticker": "AAPL", "sleeve": SAT_QV, "target_weight": 0.15}], {SAT_QV})
    replace_target_sleeves([], {SAT_QV})                     # clear it
    assert get_target_allocation(sleeve=SAT_QV) == []


def test_bigbet_cap_validation():
    import scripts.set_targets as st
    assert st._bigbet_cap_errors([{"ticker": "X", "target_weight": 0.025}]) == []
    assert any("X" in e for e in st._bigbet_cap_errors([{"ticker": "X", "target_weight": 0.04}]))
    over = st._bigbet_cap_errors([{"ticker": "X", "target_weight": 0.03},
                                  {"ticker": "Y", "target_weight": 0.03}])
    assert any("aggregate" in e for e in over)
