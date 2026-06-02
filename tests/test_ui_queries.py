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
        query_benchmark_returns,
        query_capital_weighted_roi,
        query_distinct_trade_log_run_ids,
        query_held_symbols,
        query_symbol_options,
        query_tax_breakdown,
        query_trade_log,
        query_trade_log_filter_options,
        query_trade_summary,
    )

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("data.database._engine", engine)

    for fn in (query_trade_log, query_trade_summary, query_tax_breakdown,
               query_trade_log_filter_options, query_distinct_trade_log_run_ids,
               query_benchmark_returns, query_capital_weighted_roi,
               query_held_symbols, query_symbol_options):
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


# ─────────────────────────────────────────────────────────────────────────────
# query_benchmark_returns — Page 10 benchmark-relative section helper
# ─────────────────────────────────────────────────────────────────────────────

class TestQueryBenchmarkReturns:
    """Helper for the Page 10 Benchmark-Relative Performance section.

    Built on top of query_trade_log, so it inherits the same dedup +
    active-universe semantics — but adds:
      - drop rows where benchmark_return_pct IS NULL
      - add an ``excess_pct`` = pnl_pct − benchmark_return_pct column
    fold_end rows are NOT filtered at this layer (the page decides).
    """

    def test_excess_return_uses_net_pnl(self, mem_engine):
        """Regression canary for the double-count footgun.

        ``excess_pct`` must equal ``pnl_pct − benchmark_return_pct`` exactly.
        It must NOT subtract costs_charged again — pnl_pct is already net per
        the Phase A storage convention.  The buggy formula would be:
            excess = (pnl_pct − costs_pct) − benchmark_return_pct
        which subtracts fees twice (once in pnl_pct upstream, once here).
        Pinning the correct formula prevents the asymmetric-comparison bug
        from sneaking back in alongside future Page 10 refactors.
        """
        from data.database import TradeLog
        from data.ui_queries import query_benchmark_returns

        # Trade: -2.4% net, 0.4% costs, SPY did +1.0% over the same period.
        # Correct excess: -0.024 − 0.010 = -0.034
        # Buggy   excess: (-0.024 − 0.004) − 0.010 = -0.038
        with Session(mem_engine) as session:
            session.add(TradeLog(
                source="walk_forward", run_id="r1", fold_index=0,
                symbol="SPY", signal="BUY",
                entry_ts=_now(), exit_ts=_now() + timedelta(days=2),
                entry_px=100.0, exit_px=97.6,
                exit_reason="stop", shares=10.0,
                pnl=-24.0, pnl_pct=-0.024,
                costs_charged=4.0,
                benchmark_return_pct=0.010,
                recorded_at=_now(),
            ))
            session.commit()

        df = query_benchmark_returns(
            dedup_to_latest_run=False, active_universe_only=False,
        )
        assert len(df) == 1
        row = df.iloc[0]
        assert row["excess_pct"] == pytest.approx(-0.034, abs=1e-9)
        # Specifically NOT the double-counted form:
        bug = (row["pnl_pct"] - row["costs_charged"] / (row["entry_px"] * row["shares"])) - row["benchmark_return_pct"]
        assert row["excess_pct"] != pytest.approx(bug), (
            "excess_pct is double-counting costs — Page 10 bug has regressed"
        )

    def test_drops_null_benchmark_rows(self, mem_engine):
        """Rows with NULL benchmark_return_pct must be filtered out, not
        silently zeroed.  A zero default would distort aggregates: a 0-return
        benchmark vs a non-zero trade return would appear as pure alpha when
        in fact we just don't know what the benchmark did over that window.
        """
        from data.database import TradeLog
        from data.ui_queries import query_benchmark_returns

        with Session(mem_engine) as session:
            # Populated row — should appear.
            session.add(TradeLog(
                source="walk_forward", run_id="r1", fold_index=0,
                symbol="AAPL", signal="BUY",
                entry_ts=_now(), exit_ts=_now() + timedelta(days=1),
                entry_px=200.0, exit_px=210.0,
                exit_reason="tp", shares=1.0,
                pnl=10.0, pnl_pct=0.05, costs_charged=0.5,
                benchmark_return_pct=0.01,
                recorded_at=_now(),
            ))
            # NULL row — should be dropped.
            session.add(TradeLog(
                source="walk_forward", run_id="r2", fold_index=0,
                symbol="MSFT", signal="BUY",
                entry_ts=_now(), exit_ts=_now() + timedelta(days=1),
                entry_px=300.0, exit_px=310.0,
                exit_reason="tp", shares=1.0,
                pnl=10.0, pnl_pct=0.033, costs_charged=0.5,
                benchmark_return_pct=None,
                recorded_at=_now(),
            ))
            session.commit()

        df = query_benchmark_returns(
            dedup_to_latest_run=False, active_universe_only=False,
        )
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "AAPL"

    def test_benchmark_section_handles_empty_trade_log(self, mem_engine):
        """Empty trade_log returns an empty DataFrame — never crashes.

        The Page 10 section checks ``if bench_df.empty`` and renders an
        st.info() empty state.  This pins the helper's contract: empty
        in → empty out, with the right shape (so the page's .empty check
        works regardless of whether the column exists yet).
        """
        from data.ui_queries import query_benchmark_returns

        df = query_benchmark_returns(
            dedup_to_latest_run=False, active_universe_only=False,
        )
        assert df.empty


# ─────────────────────────────────────────────────────────────────────────────
# query_held_symbols / query_symbol_options — sidebar picker symbol list
#
# The Model Signals (and every other page's) symbol picker sources its options
# from query_symbol_options, which lists the active Stage 3 universe.  Held
# positions that have been rotated out of the universe (e.g. VRT, dropped
# 2026-05-24 but still net-long 120sh) were silently absent from the dropdown
# even though signal_runner still evaluates them daily via
# _fetch_held_long_symbols.  query_held_symbols derives "currently held" from
# the live fill_log (net BUY-SELL > 0) — a DB-only proxy so the shared picker
# never has to touch IBKR — and query_symbol_options unions it on top.
# ─────────────────────────────────────────────────────────────────────────────

def _seed_fill(engine, *, symbol: str, side: str, shares: float,
               exec_id: str, exec_time: datetime | None = None,
               price: float = 100.0) -> None:
    """Insert one fill_log row (one IBKR Execution)."""
    from data.database import FillLog

    exec_time = exec_time or _now()
    with Session(engine) as session:
        session.add(FillLog(
            exec_id=exec_id,
            symbol=symbol,
            side=side,
            order_type="MKT",
            shares=shares,
            price=price,
            exec_time=exec_time,
            recorded_at=exec_time,
        ))
        session.commit()


class TestHeldSymbolPicker:

    def test_empty_fill_log_returns_no_held(self, mem_engine):
        """Pre-Phase-B databases (no fills) must yield an empty held list,
        never crash."""
        from data.ui_queries import query_held_symbols

        assert query_held_symbols() == []

    def test_net_long_symbol_is_held(self, mem_engine):
        """A symbol with SUM(BUY) - SUM(SELL) > 0 across its fills is held.

        Mirrors VRT's real shape: three partial BUY fills, no SELL.
        """
        from data.ui_queries import query_held_symbols

        _seed_fill(mem_engine, symbol="VRT", side="BUY", shares=40, exec_id="v1")
        _seed_fill(mem_engine, symbol="VRT", side="BUY", shares=50, exec_id="v2")
        _seed_fill(mem_engine, symbol="VRT", side="BUY", shares=30, exec_id="v3")

        assert query_held_symbols() == ["VRT"]

    def test_net_flat_symbol_not_held(self, mem_engine):
        """A fully-closed position (BUY shares == SELL shares) is net-flat and
        must NOT count as held — otherwise the picker keeps showing symbols the
        user has already exited."""
        from data.ui_queries import query_held_symbols

        _seed_fill(mem_engine, symbol="FLAT", side="BUY",  shares=100, exec_id="f1")
        _seed_fill(mem_engine, symbol="FLAT", side="SELL", shares=100, exec_id="f2")

        assert query_held_symbols() == []

    def test_partially_closed_symbol_still_held(self, mem_engine):
        """A position reduced but not flattened (net shares still > 0) remains
        held."""
        from data.ui_queries import query_held_symbols

        _seed_fill(mem_engine, symbol="PART", side="BUY",  shares=100, exec_id="p1")
        _seed_fill(mem_engine, symbol="PART", side="SELL", shares=40,  exec_id="p2")

        assert query_held_symbols() == ["PART"]

    def test_held_symbol_unioned_into_picker_options(self, mem_engine):
        """The headline fix: a held symbol absent from the active universe is
        still offered by the sidebar picker (the VRT case)."""
        from data.ui_queries import query_symbol_options

        # Active universe = AAPL only; VRT held but rotated out.
        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_fill(mem_engine, symbol="VRT", side="BUY", shares=120, exec_id="v1")

        opts = query_symbol_options()
        assert "AAPL" in opts            # universe symbol still present
        assert "VRT" in opts             # held-but-departed symbol now present
        # Sorted, no duplicates.
        assert opts == sorted(set(opts))

    def test_held_symbol_in_universe_not_duplicated(self, mem_engine):
        """A symbol that is BOTH active-universe and held appears exactly
        once — the union must dedupe."""
        from data.ui_queries import query_symbol_options

        _seed_universe(mem_engine, ["AAPL"], active=True)
        _seed_fill(mem_engine, symbol="AAPL", side="BUY", shares=10, exec_id="a1")

        opts = query_symbol_options()
        assert opts.count("AAPL") == 1

    def test_picker_unions_held_onto_watchlist_fallback(self, mem_engine, monkeypatch):
        """When universe_assets is empty, options fall back to the watchlist —
        and held symbols are still unioned on top of that fallback."""
        from data.ui_queries import query_symbol_options
        from config.settings import config

        monkeypatch.setattr(config.data, "watchlist", ["MSFT"])
        _seed_fill(mem_engine, symbol="VRT", side="BUY", shares=120, exec_id="v1")

        opts = query_symbol_options()
        assert set(opts) == {"MSFT", "VRT"}


# ─────────────────────────────────────────────────────────────────────────────
# query_capital_weighted_roi — Page 10 "Capital-Weighted ROI vs benchmark"
# ─────────────────────────────────────────────────────────────────────────────

def _seed_live_trade(
    engine,
    *,
    symbol: str = "AAPL",
    entry_px: float = 100.0,
    shares: float = 10.0,
    pnl_pct: float = 0.05,
    benchmark_return_pct: float | None = 0.02,
    source: str = "live",
    run_id: str = "live-run",
    exit_reason: str = "tp",
) -> None:
    """Insert one trade_log row with an explicit benchmark return.

    ``net_pnl`` (== trade_log.pnl) is stored as ``pnl_pct × entry_px × shares``
    — the WF/Phase-B net-dollar convention.  Used by the capital-weighted ROI
    tests, which the shared ``_seed_trade`` helper can't cover (it has no
    benchmark_return_pct parameter).
    """
    from data.database import TradeLog

    entry_ts = _now()
    exit_ts  = entry_ts + timedelta(days=3)
    exit_px  = entry_px * (1.0 + pnl_pct)

    with Session(engine) as session:
        session.add(TradeLog(
            source=source, run_id=run_id, fold_index=0,
            symbol=symbol, signal="BUY",
            entry_ts=entry_ts, entry_px=entry_px,
            exit_ts=exit_ts, exit_px=exit_px,
            exit_reason=exit_reason, shares=shares,
            pnl=pnl_pct * entry_px * shares,
            pnl_pct=pnl_pct,
            costs_charged=0.0,
            benchmark_return_pct=benchmark_return_pct,
            recorded_at=entry_ts,
        ))
        session.commit()


class TestCapitalWeightedROI:

    def test_empty_when_no_live_rows(self, mem_engine):
        """All-zero dict when trade_log has no live rows."""
        from data.ui_queries import query_capital_weighted_roi

        _seed_trade(mem_engine, symbol="AAPL", source="walk_forward")
        roi = query_capital_weighted_roi(active_universe_only=False)
        assert roi["n_trades"] == 0
        assert roi["strategy_roi"] == 0.0
        assert roi["benchmark_roi"] == 0.0

    def test_excludes_walk_forward_even_when_present(self, mem_engine):
        """WF rows are ignored even though they sit in the same table —
        the section is forced to source='live'."""
        from data.ui_queries import query_capital_weighted_roi

        _seed_trade(mem_engine, symbol="AAPL", source="walk_forward")
        _seed_live_trade(mem_engine, symbol="MSFT", entry_px=100.0, shares=10.0,
                         pnl_pct=0.05, benchmark_return_pct=0.02)

        roi = query_capital_weighted_roi(active_universe_only=False)
        assert roi["n_trades"] == 1  # only the live MSFT row

    def test_capital_weighting_math(self, mem_engine):
        """Big position dominates the ROI; both sides share the same base.

        Trade A: $100 × 100sh = $10,000 capital, +5% net  → +$500 strategy,
                 benchmark +1% → +$100.
        Trade B: $100 ×   1sh = $100 capital,    -10% net → -$10 strategy,
                 benchmark +1% → +$1.

        Σ capital      = 10,100
        Σ strategy_pnl = 490
        Σ benchmark    = 101
        strategy_roi   = 490 / 10100  ≈ 0.0485
        benchmark_roi  = 101 / 10100  ≈ 0.0100
        dollar_diff    = 490 − 101 = 389
        """
        from data.ui_queries import query_capital_weighted_roi

        _seed_live_trade(mem_engine, symbol="AAA", entry_px=100.0, shares=100.0,
                         pnl_pct=0.05, benchmark_return_pct=0.01)
        _seed_live_trade(mem_engine, symbol="BBB", entry_px=100.0, shares=1.0,
                         pnl_pct=-0.10, benchmark_return_pct=0.01)

        roi = query_capital_weighted_roi(active_universe_only=False)
        assert roi["n_trades"] == 2
        assert roi["capital_deployed"] == pytest.approx(10_100.0)
        assert roi["strategy_pnl"] == pytest.approx(490.0)
        assert roi["benchmark_pnl"] == pytest.approx(101.0)
        assert roi["strategy_roi"] == pytest.approx(490.0 / 10_100.0)
        assert roi["benchmark_roi"] == pytest.approx(101.0 / 10_100.0)
        assert roi["dollar_diff"] == pytest.approx(389.0)
        assert roi["roi_diff_pct"] == pytest.approx(
            490.0 / 10_100.0 - 101.0 / 10_100.0
        )

    def test_null_benchmark_rows_excluded_from_both_sides(self, mem_engine):
        """A live row missing benchmark_return_pct must not enter the capital
        base — otherwise the benchmark side would understate ROI on a base it
        didn't contribute to."""
        from data.ui_queries import query_capital_weighted_roi

        _seed_live_trade(mem_engine, symbol="AAA", entry_px=100.0, shares=10.0,
                         pnl_pct=0.05, benchmark_return_pct=0.02)
        _seed_live_trade(mem_engine, symbol="BBB", entry_px=999.0, shares=50.0,
                         pnl_pct=0.05, benchmark_return_pct=None)

        roi = query_capital_weighted_roi(active_universe_only=False)
        assert roi["n_trades"] == 1                       # BBB excluded
        assert roi["capital_deployed"] == pytest.approx(1_000.0)  # only AAA's 100×10

    def test_strategy_side_is_net_not_double_counted(self, mem_engine):
        """strategy_pnl must equal stored net pnl, NOT pnl − costs_charged
        (the double-count footgun)."""
        from data.ui_queries import query_capital_weighted_roi

        # pnl_pct net = +0.05 on $1,000 capital → +$50 net.  costs already baked in.
        _seed_live_trade(mem_engine, symbol="AAA", entry_px=100.0, shares=10.0,
                         pnl_pct=0.05, benchmark_return_pct=0.0)
        roi = query_capital_weighted_roi(active_universe_only=False)
        assert roi["strategy_pnl"] == pytest.approx(50.0)
        # benchmark 0% → strategy_roi is the full edge
        assert roi["dollar_diff"] == pytest.approx(50.0)
