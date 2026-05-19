"""
Regression tests for trade_log P&L accounting in data/ui_queries.py.

Background: Phase A's WF bracket simulator stores ``pnl = pnl_pct × entry_px ×
shares`` and ``pnl_pct = gross_pct − total_costs``.  That makes the stored
``pnl`` field the *net* dollar P&L — costs are already deducted.

The original Page 10 logic had ``net_pnl = pnl − costs_charged``, which
double-counted fees (subtracting costs from a number that already excluded
them).  Verified live against a SPY run on 2026-05-07: a net −$966.79 trade
displayed as −$1,127.10.

These tests pin the corrected accounting:
  net_pnl   = pnl                    (already net per Phase A semantics)
  gross_pnl = pnl + costs_charged    (back-derived for display)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


# ── Shared in-memory DB fixture ───────────────────────────────────────────────

@pytest.fixture()
def mem_engine(monkeypatch):
    """Replace get_engine() with an in-memory SQLite engine for each test.

    Also clears Streamlit's @st.cache_data on the trade-log query helpers —
    the decorator memoises by argument value, and our tests call with the
    same args (``dedup_to_latest_run=False``).  Without clearing, the second
    test would receive the first test's cached DataFrame even though
    ``mem_engine`` swapped to a fresh DB.
    """
    from data.database import Base
    from data.ui_queries import (
        query_distinct_trade_log_run_ids,
        query_tax_breakdown,
        query_trade_log,
        query_trade_log_filter_options,
        query_trade_summary,
    )

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("data.database._engine", engine)

    for fn in (query_trade_log, query_trade_summary, query_tax_breakdown,
               query_trade_log_filter_options, query_distinct_trade_log_run_ids):
        try:
            fn.clear()
        except Exception:
            pass

    yield engine


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_trade(
    engine,
    *,
    symbol: str = "SPY",
    entry_px: float = 688.47,
    exit_px: float = 674.57,
    shares: float = 58.0,
    pnl_pct: float = -0.024211,        # NET return (already includes costs)
    costs_pct: float = 0.004022,       # round-trip cost as fraction of position
    entry_ts: datetime | None = None,
    exit_ts: datetime | None = None,
    source: str = "walk_forward",
    run_id: str = "test-run",
    exit_reason: str = "stop",
) -> dict:
    """Insert one trade_log row mirroring the WF simulator's storage convention.

    Returns the expected post-derivation values for assertions:
      ``stored_pnl``     — what trade_log.pnl actually contains (NET dollars)
      ``stored_costs``   — what trade_log.costs_charged actually contains (DOLLARS)
      ``expected_gross`` — what query_trade_log should report as gross_pnl
      ``expected_net``   — what query_trade_log should report as net_pnl
    """
    from data.database import TradeLog

    entry_ts = entry_ts or _now()
    exit_ts  = exit_ts  or (entry_ts + timedelta(days=1))

    stored_pnl   = pnl_pct * entry_px * shares          # NET dollars
    stored_costs = costs_pct * entry_px * shares        # DOLLAR fees

    with Session(engine) as session:
        session.add(TradeLog(
            source=source,
            run_id=run_id,
            fold_index=0,
            symbol=symbol,
            signal="BUY",
            entry_ts=entry_ts,
            entry_px=entry_px,
            exit_ts=exit_ts,
            exit_px=exit_px,
            exit_reason=exit_reason,
            shares=shares,
            pnl=stored_pnl,
            pnl_pct=pnl_pct,
            costs_charged=stored_costs,
            recorded_at=entry_ts,
        ))
        session.commit()

    return {
        "stored_pnl":     stored_pnl,
        "stored_costs":   stored_costs,
        "expected_net":   stored_pnl,                    # pnl IS net
        "expected_gross": stored_pnl + stored_costs,     # back-derived
    }


# ─────────────────────────────────────────────────────────────────────────────
# query_trade_log derived columns
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryTradeLogDerivedColumns:

    def test_net_pnl_equals_stored_pnl(self, mem_engine):
        """Regression for the Page 10 double-counting bug.

        net_pnl must equal trade_log.pnl exactly — fees were already deducted
        upstream by walk_forward.py:_close_trade.  Subtracting costs_charged
        again would understate net P&L by 2× costs.
        """
        from data.ui_queries import query_trade_log

        expected = _seed_trade(mem_engine)
        df = query_trade_log(dedup_to_latest_run=False, active_universe_only=False)
        assert len(df) == 1
        row = df.iloc[0]
        assert row["net_pnl"] == pytest.approx(expected["expected_net"])
        assert row["net_pnl"] == pytest.approx(row["pnl"])
        # Specifically NOT the buggy formula: pnl - costs_charged would be far worse.
        bug_value = row["pnl"] - row["costs_charged"]
        assert row["net_pnl"] != pytest.approx(bug_value), (
            "net_pnl is double-counting costs — Page 10 bug has regressed"
        )

    def test_gross_pnl_back_derived(self, mem_engine):
        """gross_pnl = pnl + costs_charged — recovers the pre-cost realised P&L."""
        from data.ui_queries import query_trade_log

        expected = _seed_trade(mem_engine)
        df = query_trade_log(dedup_to_latest_run=False, active_universe_only=False)
        row = df.iloc[0]
        assert row["gross_pnl"] == pytest.approx(expected["expected_gross"])
        assert row["gross_pnl"] == pytest.approx(row["pnl"] + row["costs_charged"])

    def test_gross_minus_costs_equals_net(self, mem_engine):
        """Sanity: the three columns must satisfy gross − costs == net."""
        from data.ui_queries import query_trade_log

        _seed_trade(mem_engine)
        df = query_trade_log(dedup_to_latest_run=False, active_universe_only=False)
        row = df.iloc[0]
        assert row["gross_pnl"] - row["costs_charged"] == pytest.approx(row["net_pnl"])

    def test_winning_trade_accounting(self, mem_engine):
        """Same invariants on a winning trade.

        Anchored to the SPY trade 2 from the 2026-05-07 verification:
        entry=$673.22, exit=$676.42, 59 shares, pnl_pct=+0.27%.
        Expected: net=+$108.85, gross=+$188.88, costs=$80.03.
        """
        from data.ui_queries import query_trade_log

        expected = _seed_trade(
            mem_engine,
            entry_px=673.22, exit_px=676.42, shares=59.0,
            pnl_pct=0.0027404,   # NET (post-cost)
            costs_pct=0.002014,
            exit_reason="fold_end",
        )
        df = query_trade_log(dedup_to_latest_run=False, active_universe_only=False)
        row = df.iloc[0]

        assert row["net_pnl"]   == pytest.approx(expected["expected_net"],   abs=0.01)
        assert row["gross_pnl"] == pytest.approx(expected["expected_gross"], abs=0.01)
        # Concrete dollar checks — guards against silent semantic flips.
        assert row["net_pnl"]   == pytest.approx(108.85, abs=0.05)
        assert row["gross_pnl"] == pytest.approx(188.88, abs=0.05)


# ─────────────────────────────────────────────────────────────────────────────
# query_trade_summary aggregates
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryTradeSummary:

    def test_summary_uses_gross_pnl_column_not_pnl(self, mem_engine):
        """Summary cards' Gross P&L must come from the back-derived gross_pnl,
        not from raw ``pnl`` (which would display the net number under a Gross
        label — the original bug)."""
        from data.ui_queries import query_trade_summary

        e1 = _seed_trade(mem_engine, run_id="r1")
        e2 = _seed_trade(
            mem_engine, run_id="r2",
            entry_px=673.22, exit_px=676.42, shares=59.0,
            pnl_pct=0.0027404, costs_pct=0.002014,
            exit_reason="fold_end",
        )

        s = query_trade_summary(dedup_to_latest_run=False, active_universe_only=False)
        assert s["n_trades"] == 2

        expected_net   = e1["expected_net"]   + e2["expected_net"]
        expected_gross = e1["expected_gross"] + e2["expected_gross"]
        expected_costs = e1["stored_costs"]   + e2["stored_costs"]

        assert s["net_pnl"]     == pytest.approx(expected_net,   abs=0.01)
        assert s["gross_pnl"]   == pytest.approx(expected_gross, abs=0.01)
        assert s["total_costs"] == pytest.approx(expected_costs, abs=0.01)
        # The headline invariant: gross − costs == net for the aggregate too.
        assert s["gross_pnl"] - s["total_costs"] == pytest.approx(s["net_pnl"], abs=0.01)

    def test_win_rate_uses_net_pnl_signs(self, mem_engine):
        """Win rate counts a trade as a win iff net_pnl > 0 — fees count
        against you.  A trade that breaks even gross but loses to fees is a
        loser.  This anchors the 'net' semantic against the bug-era 'gross'
        interpretation."""
        from data.ui_queries import query_trade_summary

        # Trade A: clear winner — gross +$200, costs $20, net +$180.
        _seed_trade(
            mem_engine, run_id="winner",
            entry_px=100.0, exit_px=102.0, shares=100.0,
            pnl_pct=0.018,     # NET
            costs_pct=0.002,   # ~$20 of costs on a $10k position
            exit_reason="tp",
        )
        # Trade B: gross +$5 but costs $20 → net −$15.  Under the bug-era
        # interpretation (treating pnl as gross), this would have counted as
        # a win; with the fix it correctly counts as a loss.
        _seed_trade(
            mem_engine, run_id="fee_loser",
            entry_px=100.0, exit_px=100.05, shares=100.0,
            pnl_pct=-0.0015,   # NET: small loss after fees
            costs_pct=0.002,
            exit_reason="signal_flip",
        )

        s = query_trade_summary(dedup_to_latest_run=False, active_universe_only=False)
        assert s["n_trades"] == 2
        # Exactly 1 net winner — the fee_loser is correctly classified as a loss.
        assert s["win_rate"] == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# active_universe_only filter
#
# Page 10's "Active universe only" checkbox (default ON) drops walk_forward
# rows for symbols no longer tracked, but always passes through live rows so
# real broker fills stay in the historical record for tax purposes.  Without
# this filter, the Page 10 Summary aggregates over a mix of current-universe
# and historical-only symbols, inflating totals and mixing time periods.
# ─────────────────────────────────────────────────────────────────────────────

def _seed_universe(engine, symbols: list[str], active: bool = True) -> None:
    """Insert `universe_assets` rows so `_filter_to_active_universe` sees them."""
    from data.database import UniverseAsset

    with Session(engine) as session:
        for sym in symbols:
            session.add(UniverseAsset(
                symbol=sym,
                name=sym,
                asset_class="us_equity",
                exchange="TEST",
                is_fixture=False,
                stage=3,
                active=active,
                added_at=_now(),
            ))
        session.commit()


class TestActiveUniverseFilter:

    def test_walk_forward_row_in_universe_kept(self, mem_engine):
        """WF row whose symbol is in the active universe survives the filter."""
        from data.ui_queries import query_trade_log

        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_trade(mem_engine, symbol="AAPL", source="walk_forward")

        df = query_trade_log(active_universe_only=True, dedup_to_latest_run=False)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "AAPL"

    def test_walk_forward_row_not_in_universe_dropped(self, mem_engine):
        """WF row for a departed symbol is dropped when the filter is ON."""
        from data.ui_queries import query_trade_log

        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_trade(mem_engine, symbol="AAPL", source="walk_forward", run_id="r-keep")
        _seed_trade(mem_engine, symbol="MMM",  source="walk_forward", run_id="r-drop")

        df_on  = query_trade_log(active_universe_only=True,  dedup_to_latest_run=False)
        df_off = query_trade_log(active_universe_only=False, dedup_to_latest_run=False)

        assert set(df_on["symbol"])  == {"AAPL"}
        assert set(df_off["symbol"]) == {"AAPL", "MMM"}

    def test_live_row_always_passes_through(self, mem_engine):
        """Live broker fills must survive the filter even when their symbol
        has rotated out of the universe — required for tax-reporting fidelity."""
        from data.ui_queries import query_trade_log

        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_trade(mem_engine, symbol="MMM", source="live", run_id="r-live")

        df = query_trade_log(active_universe_only=True, dedup_to_latest_run=False)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "MMM"
        assert df.iloc[0]["source"] == "live"

    def test_empty_universe_falls_back_to_watchlist(self, mem_engine, monkeypatch):
        """When `universe_assets` is empty, the filter falls back to
        `config.data.watchlist` so static-watchlist mode still works."""
        from data.ui_queries import query_trade_log
        from config.settings import config

        # No universe_assets rows seeded — relies on watchlist fallback.
        monkeypatch.setattr(config.data, "watchlist", ["WLST"])

        _seed_trade(mem_engine, symbol="WLST", source="walk_forward", run_id="r-w")
        _seed_trade(mem_engine, symbol="GONE", source="walk_forward", run_id="r-g")

        df = query_trade_log(active_universe_only=True, dedup_to_latest_run=False)
        assert set(df["symbol"]) == {"WLST"}

    def test_summary_aggregates_only_active_universe(self, mem_engine):
        """The Summary cards' totals must come from active-universe trades
        only when the filter is ON — this is the headline fix.  Off would
        include the departed symbol and inflate net P&L."""
        from data.ui_queries import query_trade_summary

        _seed_universe(mem_engine, ["AAPL"], active=True)

        # AAPL: a clear winner under the active universe.
        keeper = _seed_trade(
            mem_engine, symbol="AAPL", run_id="r-keep",
            entry_px=100.0, exit_px=110.0, shares=100.0,
            pnl_pct=0.095, costs_pct=0.005, exit_reason="tp",
        )
        # MMM: a departed-symbol winner that should NOT show up in totals.
        _seed_trade(
            mem_engine, symbol="MMM", run_id="r-drop",
            entry_px=100.0, exit_px=120.0, shares=100.0,
            pnl_pct=0.195, costs_pct=0.005, exit_reason="tp",
        )

        s_on  = query_trade_summary(active_universe_only=True,  dedup_to_latest_run=False)
        s_off = query_trade_summary(active_universe_only=False, dedup_to_latest_run=False)

        assert s_on["n_trades"] == 1
        assert s_on["net_pnl"] == pytest.approx(keeper["expected_net"], abs=0.01)

        assert s_off["n_trades"] == 2
        # OFF must produce strictly larger net P&L (MMM is a winner too).
        assert s_off["net_pnl"] > s_on["net_pnl"]

    def test_filter_options_match_view(self, mem_engine):
        """The dropdown symbol list must reflect the same active-universe
        filter as the table itself — otherwise users see selectable symbols
        that yield empty tables (the same UX bug that 2026-05-04 fixed for
        the dedup case)."""
        from data.ui_queries import query_trade_log_filter_options

        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_trade(mem_engine, symbol="AAPL", source="walk_forward", run_id="r-a")
        _seed_trade(mem_engine, symbol="MMM",  source="walk_forward", run_id="r-m")

        opts_on  = query_trade_log_filter_options(active_universe_only=True,
                                                  dedup_to_latest_run=False)
        opts_off = query_trade_log_filter_options(active_universe_only=False,
                                                  dedup_to_latest_run=False)
        assert opts_on["symbols"]  == ["AAPL"]
        assert set(opts_off["symbols"]) == {"AAPL", "MMM"}
