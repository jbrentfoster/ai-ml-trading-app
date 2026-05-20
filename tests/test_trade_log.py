"""
Tests for trade_log benchmark-return backfill (scripts/backfill_benchmark_returns.py).

Two groups:
1. Backfill mechanics (mocked in-memory SQLite — fast, deterministic):
     - benchmark_return_pct = (spy_exit / spy_entry) - 1 to 6 decimal places
     - rows with no SPY bar on entry_ts or exit_ts get NULL, not 0 or a crash
     - re-running the backfill is a no-op for already-populated rows

2. Baseline-pin canaries against the production db/trading.db (skipped when
   the file doesn't exist — keeps CI clean):
     - test_benchmark_aggregates_deduped_baseline_2026_05_19
     - test_benchmark_aggregates_raw_baseline_2026_05_19
   These deliberately fail after each weekly --force retrain (which inserts
   fresh trade_log rows under new run_ids).  The failure IS the canary —
   eyeball the new aggregates, re-pin to the new date, ship.  See the
   "Dedup vs raw views are honest answers to different questions"
   architectural-decision note in CLAUDE.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


# ── Shared in-memory DB fixture (mirrors test_ui_queries.py pattern) ──────────

@pytest.fixture()
def mem_engine(monkeypatch):
    """Replace get_engine() with an in-memory SQLite engine for each test."""
    from data.database import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_spy_bars(engine, closes_by_date: dict) -> None:
    """Insert SPY daily bars from a {date -> close} mapping.

    Bar timestamps are stamped at 04:00 UTC to match the project's
    US-daily convention (so backfill normalisation via .normalize() resolves
    them to the right calendar date).
    """
    from data.database import OHLCVBar

    with Session(engine) as session:
        for d, close in closes_by_date.items():
            ts = pd.Timestamp(d).normalize() + pd.Timedelta(hours=4)
            session.add(OHLCVBar(
                symbol="SPY", interval="1d",
                timestamp=ts.to_pydatetime(),
                open=close, high=close, low=close, close=close,
                volume=1_000_000.0,
            ))
        session.commit()


def _seed_trade(
    engine,
    *,
    symbol: str = "AAPL",
    entry_date: str,
    exit_date: str,
    pnl_pct: float = 0.05,
    benchmark_return_pct: float | None = None,
) -> None:
    """Insert one trade_log row with explicit entry/exit dates."""
    from data.database import TradeLog

    entry_ts = pd.Timestamp(entry_date).normalize() + pd.Timedelta(hours=4)
    exit_ts  = pd.Timestamp(exit_date).normalize()  + pd.Timedelta(hours=4)
    entry_px = 100.0
    exit_px  = entry_px * (1.0 + pnl_pct)

    with Session(engine) as session:
        session.add(TradeLog(
            source="walk_forward", run_id="test-run", fold_index=0,
            symbol=symbol, signal="BUY",
            entry_ts=entry_ts.to_pydatetime(),
            entry_px=entry_px,
            exit_ts=exit_ts.to_pydatetime(),
            exit_px=exit_px,
            exit_reason="tp", shares=1.0,
            pnl=pnl_pct * entry_px, pnl_pct=pnl_pct, costs_charged=0.0,
            benchmark_return_pct=benchmark_return_pct,
            recorded_at=_now(),
        ))
        session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Backfill mechanics
# ─────────────────────────────────────────────────────────────────────────────

class TestBackfillMechanics:

    def test_benchmark_return_computed_from_spy_bars(self, mem_engine):
        """benchmark_return_pct = (spy_exit / spy_entry) - 1, six decimal places."""
        from scripts.backfill_benchmark_returns import backfill

        # SPY moves from 500 → 525 over the same window the trade is in.
        # Expected benchmark return = 525/500 - 1 = 0.05 exactly.
        _seed_spy_bars(mem_engine, {
            "2026-05-01": 500.0,
            "2026-05-08": 525.0,
        })
        _seed_trade(
            mem_engine,
            entry_date="2026-05-01", exit_date="2026-05-08",
            pnl_pct=0.03,
        )

        summary = backfill(benchmark="SPY")
        assert summary == {
            "benchmark": "SPY",
            "n_candidates": 1,
            "n_updated": 1,
            "n_missing_bar": 0,
        }

        from data.database import get_trade_log
        df = get_trade_log()
        assert len(df) == 1
        assert df.iloc[0]["benchmark_return_pct"] == pytest.approx(0.05, abs=1e-6)

    def test_benchmark_return_null_when_spy_bar_missing(self, mem_engine):
        """Trade whose entry_ts has no SPY bar gets NULL, not 0 and not a crash."""
        from scripts.backfill_benchmark_returns import backfill

        # SPY bar exists only on the exit side — entry has no bar.
        _seed_spy_bars(mem_engine, {
            "2026-05-08": 525.0,
            # 2026-05-01 deliberately missing
        })
        _seed_trade(
            mem_engine,
            entry_date="2026-05-01", exit_date="2026-05-08",
        )

        summary = backfill(benchmark="SPY")
        assert summary["n_candidates"]  == 1
        assert summary["n_updated"]     == 0
        assert summary["n_missing_bar"] == 1

        from data.database import get_trade_log
        df = get_trade_log()
        assert len(df) == 1
        assert df.iloc[0]["benchmark_return_pct"] is None or pd.isna(
            df.iloc[0]["benchmark_return_pct"]
        )

    def test_backfill_idempotent(self, mem_engine):
        """Running backfill twice must produce identical results — second run
        is a no-op against already-populated rows.

        Guards against the bug where a logic change (e.g. SPY-bar refresh that
        slightly tweaks the close) silently re-writes already-correct values
        and drifts the historical record."""
        from scripts.backfill_benchmark_returns import backfill

        _seed_spy_bars(mem_engine, {
            "2026-05-01": 500.0,
            "2026-05-08": 525.0,
        })
        _seed_trade(
            mem_engine,
            entry_date="2026-05-01", exit_date="2026-05-08",
        )

        # First run — populates the column.
        first = backfill(benchmark="SPY")
        assert first["n_updated"] == 1
        assert first["n_candidates"] == 1

        from data.database import get_trade_log
        df1 = get_trade_log()
        value_after_first = df1.iloc[0]["benchmark_return_pct"]

        # Second run — must find no candidates (NULL filter excludes
        # already-populated rows).
        second = backfill(benchmark="SPY")
        assert second["n_candidates"] == 0
        assert second["n_updated"]    == 0

        df2 = get_trade_log()
        assert df2.iloc[0]["benchmark_return_pct"] == pytest.approx(
            value_after_first, abs=1e-9
        )


# ─────────────────────────────────────────────────────────────────────────────
# Baseline-pin canaries (production DB — skip if absent)
# ─────────────────────────────────────────────────────────────────────────────
#
# These tests deliberately fail when the production data drifts (next weekly
# --force retrain inserts fresh rows under new run_ids → both the deduped and
# raw aggregates shift).  The failure is the *canary firing*: eyeball the new
# numbers, confirm nothing pathological happened (e.g. SPY ingestion broke or
# the backfill skipped rows), and re-pin to the new baseline + date.

_PROD_DB = Path(__file__).resolve().parent.parent / "db" / "trading.db"


@pytest.mark.skipif(
    not _PROD_DB.exists(),
    reason=f"Production DB ({_PROD_DB}) not present — skip baseline pins.",
)
class TestBenchmarkAggregatesBaseline20260519:
    """Snapshot of Page 10's benchmark-relative aggregates on 2026-05-19.

    Both views computed over the same fold_end-excluded slice — the divergence
    between them is the architectural finding documented in CLAUDE.md ("Dedup
    vs raw views are honest answers to different questions").
    """

    @pytest.fixture(autouse=True)
    def _use_prod_db(self, monkeypatch):
        """Reset the engine cache so we read the real db/trading.db."""
        # Wipe any in-memory engine cached from earlier tests in this session.
        monkeypatch.setattr("data.database._engine", None)
        # Clear @st.cache_data on the helpers we'll exercise.
        from data.ui_queries import query_benchmark_returns, query_trade_log
        try:
            query_benchmark_returns.clear()
            query_trade_log.clear()
        except Exception:
            pass

    def _strategy_metrics(self, *, dedup: bool, active_universe: bool) -> dict:
        from data.ui_queries import query_benchmark_returns
        # Bypass @st.cache_data: clearing alone is insufficient if a prior test
        # called the wrapped function with the same args.
        df = query_benchmark_returns.__wrapped__(
            dedup_to_latest_run=dedup,
            active_universe_only=active_universe,
        )
        strategy = df[df["exit_reason"] != "fold_end"]
        n = len(strategy)
        if n == 0:
            return {"n": 0, "cum_excess_pct": 0.0, "win_rate_vs_bench": 0.0}
        return {
            "n":                 n,
            "cum_excess_pct":    float(strategy["excess_pct"].sum())  * 100.0,
            "win_rate_vs_bench": 100.0 * (strategy["excess_pct"] > 0).sum() / n,
        }

    def test_benchmark_aggregates_deduped_baseline_2026_05_19(self):
        """Default page view (dedup=ON, active_universe=ON), fold_end excluded.

        Pin date: 2026-05-19.  Tolerances are wide enough to absorb
        floating-point recomputation but tight enough to catch a real shift.
        FAILURE EXPECTED after the next weekly retrain — re-pin to new
        numbers and bump the date in the test name."""
        m = self._strategy_metrics(dedup=True, active_universe=True)
        assert m["n"]                 == 49,                       (
            f"Row count drifted from 2026-05-19 baseline of 49 — got {m['n']}.  "
            "Likely cause: a new weekly --force retrain has landed.  Eyeball "
            "the new numbers (Page 10 default view) and re-pin this test."
        )
        assert m["cum_excess_pct"]    == pytest.approx(+124.69, abs=0.5)
        assert m["win_rate_vs_bench"] == pytest.approx(  51.0,  abs=0.5)

    def test_benchmark_aggregates_raw_baseline_2026_05_19(self):
        """Multi-run view (dedup=OFF, active_universe=OFF), fold_end excluded.

        Pin date: 2026-05-19.  Same fold_end-excluded slice as the deduped
        baseline — the divergence vs the deduped numbers is the architectural
        finding (see CLAUDE.md 'Dedup vs raw views are honest answers')."""
        m = self._strategy_metrics(dedup=False, active_universe=False)
        assert m["n"]                 == 839,                      (
            f"Row count drifted from 2026-05-19 baseline of 839 — got {m['n']}.  "
            "Likely cause: a new --force retrain inserted rows OR the backfill "
            "skipped rows for some symbols.  Investigate before re-pinning."
        )
        assert m["cum_excess_pct"]    == pytest.approx(-1711.41, abs=2.0)
        assert m["win_rate_vs_bench"] == pytest.approx(   32.4,  abs=0.5)
