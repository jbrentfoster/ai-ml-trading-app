"""
Unit tests for Phase B live-fill reconciliation (execution/reconciliation.py).

All tests inject the IBKR fetch as a plain callable returning canned execution
dicts — no async / reqExecutions mocking.  In-memory SQLite via the same
mem_engine monkeypatch pattern used in test_risk.py.

Coverage:
  - pair-and-write happy path
  - partial fills aggregate (VWAP + summed shares)
  - idempotent re-run is a no-op
  - orphan entry (no matching exit) skipped with no crash
  - net-P&L convention canary (pnl is net; gross = pnl + costs_charged)
  - commission-race cost-update (deferred until commissionReport lands)
  - exit_reason session-independent paths (trailing_log / price_match / default)
  - exit_reason order_lookup (STP/LMT/TRAIL)
  - price-match tolerance regimes (low-price $0.05 floor, high-price 0.1%)
  - _to_naive_utc UTC coercion
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture()
def mem_engine(monkeypatch):
    """In-memory SQLite engine swapped in for get_engine() for each test."""
    from data.database import Base

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("data.database._engine", engine)
    yield engine


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _exec(exec_id, symbol, side, shares, price, *, t, commission=1.0,
          order_type=None, conid=111, order_id=1, account="DU123"):
    """Build a canned execution dict in the shape get_executions() returns."""
    return {
        "exec_id":         exec_id,
        "order_id":        order_id,
        "perm_id":         None,
        "parent_order_id": None,
        "account":         account,
        "symbol":          symbol,
        "conid":           conid,
        "side":            side,
        "order_type":      order_type,
        "shares":          float(shares),
        "price":           float(price),
        "commission":      commission,
        "realized_pnl":    None,
        "exec_time":       t,
    }


def _fetcher(execs):
    """Return a fetch_executions callable that ignores `since` and returns execs."""
    return lambda since: list(execs)


def _trades(symbol=None):
    from data.database import get_trade_log
    df = get_trade_log(symbol=symbol, source="live")
    return df


# ─────────────────────────────────────────────────────────────────────────────

class TestReconcileHappyPath:

    def test_pair_and_write_one_round_trip(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [
            _exec("E1", "SPY", "BUY",  10, 100.0, t=t0, order_type="LMT"),
            _exec("E2", "SPY", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  order_type="LMT"),
        ]
        res = reconcile_fills(_fetcher(execs))

        assert res.n_new_fills == 2
        assert res.n_trades_written == 1
        df = _trades("SPY")
        assert len(df) == 1
        row = df.iloc[0]
        assert row["entry_px"] == 100.0
        assert row["exit_px"] == 110.0
        assert row["shares"] == 10.0
        assert row["exit_reason"] == "tp"           # SELL LMT → tp via order_lookup
        assert row["entry_exec_id"] == "E1"
        assert row["exit_exec_id"] == "E2"

    def test_partial_fills_aggregate_vwap(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [
            _exec("B1", "AAA", "BUY",  4, 100.0, t=t0, commission=0.5),
            _exec("B2", "AAA", "BUY",  6, 105.0, t=t0 + timedelta(minutes=1),
                  commission=0.5),
            _exec("S1", "AAA", "SELL", 5, 120.0, t=t0 + timedelta(hours=1),
                  commission=0.5),
            _exec("S2", "AAA", "SELL", 5, 130.0, t=t0 + timedelta(hours=2),
                  commission=0.5, order_type="LMT"),
        ]
        res = reconcile_fills(_fetcher(execs))

        assert res.n_trades_written == 1
        row = _trades("AAA").iloc[0]
        # entry VWAP = (4*100 + 6*105)/10 = 103.0 ; exit VWAP = (5*120+5*130)/10 = 125.0
        assert row["entry_px"] == pytest.approx(103.0)
        assert row["exit_px"] == pytest.approx(125.0)
        assert row["shares"] == 10.0
        assert row["exit_exec_id"] == "S2"          # closing (latest) exit fill


class TestIdempotencyAndOrphans:

    def test_idempotent_rerun_is_noop(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [
            _exec("E1", "SPY", "BUY",  10, 100.0, t=t0, order_type="LMT"),
            _exec("E2", "SPY", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  order_type="LMT"),
        ]
        reconcile_fills(_fetcher(execs))
        res2 = reconcile_fills(_fetcher(execs))

        assert res2.n_new_fills == 0
        assert res2.n_skipped_fills == 2            # both fills already present
        assert res2.n_trades_skipped == 1           # dedup on exit_exec_id
        assert len(_trades("SPY")) == 1             # still exactly one trade

    def test_orphan_entry_skipped(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [_exec("E1", "AAPL", "BUY", 10, 100.0, t=t0, order_type="LMT")]
        res = reconcile_fills(_fetcher(execs))

        assert res.n_orphans == 1
        assert res.n_trades_written == 0
        assert _trades("AAPL").empty

    def test_lone_exit_no_entry_surfaced_as_orphan(self, mem_engine):
        """Regression for the 2026-06-05 GLW silent-drop.

        An exit fill with no matching entry in fill_log (entry aged out / never
        ingested) used to fall through both the round-trip write and the orphan
        branch (which only fired for entry_fills), vanishing with no row and no
        warning.  It must now count as an orphan (visible, recoverable).
        """
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [_exec("X1", "GLW", "SELL", 231, 183.40, t=t0, order_type=None)]
        res = reconcile_fills(_fetcher(execs))

        assert res.n_orphans == 1               # surfaced, not silently dropped
        assert res.n_trades_written == 0        # no synthetic entry fabricated
        assert _trades("GLW").empty


class TestInvertedRoundTripGuard:
    """Regression for the 2026-06-05 SLV exit-before-entry corruption.

    The aggregator collects all BUYs into entry_fills and all SELLs into
    exit_fills regardless of order, so an orphaned-short sequence (a SELL stop
    fires, then a BUY covers the next day) trips the net==flat check as a "long"
    with exit_ts < entry_ts.  Persisting it created a chronologically-impossible
    row that double-counted the loss (id=2022 dup of the correct short id=2002).
    """

    def test_sell_then_buy_does_not_write_inverted_row(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t_sell = _now() - timedelta(days=3)          # 4/29-analog STP sell
        t_buy  = t_sell + timedelta(days=1)          # 4/30-analog MKT cover
        execs = [
            _exec("STP", "SLV", "SELL", 100, 64.96, t=t_sell, order_type="STP"),
            _exec("MKT", "SLV", "BUY",  100, 66.67, t=t_buy,  order_type="MKT"),
        ]
        res = reconcile_fills(_fetcher(execs))

        assert res.n_trades_written == 0
        assert res.n_skipped_inverted == 1
        assert _trades("SLV").empty
        # Detector the 2026-06-04 verification relied on must stay clean.
        from data.database import get_trade_log
        all_live = get_trade_log(source="live")
        if not all_live.empty:
            inverted = all_live[all_live["exit_ts"] < all_live["entry_ts"]]
            assert inverted.empty

    def test_inverted_guard_stable_on_rerun(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t_sell = _now() - timedelta(days=3)
        t_buy  = t_sell + timedelta(days=1)
        execs = [
            _exec("STP", "SLV", "SELL", 100, 64.96, t=t_sell, order_type="STP"),
            _exec("MKT", "SLV", "BUY",  100, 66.67, t=t_buy,  order_type="MKT"),
        ]
        reconcile_fills(_fetcher(execs))
        res2 = reconcile_fills(_fetcher(execs))

        assert res2.n_trades_written == 0
        assert res2.n_skipped_inverted == 1          # re-rejected, never persisted
        assert _trades("SLV").empty


class TestNetPnlConvention:

    def test_pnl_is_net_canary(self, mem_engine):
        """pnl stored NET; gross = pnl + costs_charged; never pnl - costs."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        execs = [
            _exec("E1", "SPY", "BUY",  10, 100.0, t=t0, commission=1.0,
                  order_type="LMT"),
            _exec("E2", "SPY", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  commission=1.0, order_type="LMT"),
        ]
        reconcile_fills(_fetcher(execs))
        row = _trades("SPY").iloc[0]

        # commissions = 2.0 ; position_value = 1000 ; pnl_pct = 0.10 - 0.002 = 0.098
        assert row["costs_charged"] == pytest.approx(2.0)
        assert row["pnl_pct"] == pytest.approx(0.098)
        assert row["pnl"] == pytest.approx(98.0)            # net
        # canary: the double-count anti-pattern would store 96.0
        assert row["pnl"] != pytest.approx(96.0)
        gross = row["pnl"] + row["costs_charged"]
        assert gross == pytest.approx(100.0)                # (110-100)*10


class TestCommissionRace:

    def test_null_commission_deferred_then_cost_updated(self, mem_engine):
        from execution.reconciliation import reconcile_fills
        from data.database import upsert_fill, get_fills

        t0 = _now() - timedelta(days=2)
        execs_null = [
            _exec("E1", "SPY", "BUY",  10, 100.0, t=t0, commission=None,
                  order_type="LMT"),
            _exec("E2", "SPY", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  commission=None, order_type="LMT"),
        ]
        res1 = reconcile_fills(_fetcher(execs_null))
        assert res1.n_new_fills == 2
        assert res1.n_deferred_cost == 1            # withheld pending commission
        assert res1.n_trades_written == 0
        assert _trades("SPY").empty

        # commissionReport now lands.
        execs_priced = [
            _exec("E1", "SPY", "BUY",  10, 100.0, t=t0, commission=1.0,
                  order_type="LMT"),
            _exec("E2", "SPY", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  commission=1.0, order_type="LMT"),
        ]
        res2 = reconcile_fills(_fetcher(execs_priced))
        assert res2.n_cost_updated == 2             # both fills refreshed
        assert res2.n_trades_written == 1
        row = _trades("SPY").iloc[0]
        assert row["costs_charged"] == pytest.approx(2.0)
        assert row["pnl"] == pytest.approx(98.0)    # net, not the NULL-frozen 100.0

    def test_upsert_fill_returns_cost_updated(self, mem_engine):
        from data.database import upsert_fill

        t0 = _now()
        rec = _exec("X1", "SPY", "BUY", 5, 50.0, t=t0, commission=None)
        assert upsert_fill(rec) == "inserted"
        assert upsert_fill(rec) == "skipped"        # still NULL, nothing to do
        rec_priced = dict(rec, commission=0.75)
        assert upsert_fill(rec_priced) == "cost_updated"
        assert upsert_fill(rec_priced) == "skipped"  # already has a value


class TestExitReasonInference:

    def _round_trip(self, symbol, exit_px, *, order_type, t):
        return [
            _exec(f"{symbol}-B", symbol, "BUY",  10, 100.0, t=t, order_type="LMT"),
            _exec(f"{symbol}-S", symbol, "SELL", 10, exit_px, t=t + timedelta(hours=1),
                  order_type=order_type),
        ]

    def test_order_lookup_stp_tp_trail(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        for sym, otype, expected in [
            ("AAA", "STP", "stop"),
            ("BBB", "LMT", "tp"),
            ("CCC", "TRAIL", "trailing"),
        ]:
            reconcile_fills(_fetcher(self._round_trip(sym, 110.0, order_type=otype, t=t0)))
            assert _trades(sym).iloc[0]["exit_reason"] == expected

    def test_trailing_log_path(self, mem_engine):
        from execution.reconciliation import reconcile_fills
        from data.database import log_trailing_stop_action

        t0 = _now() - timedelta(days=2)
        # A CONVERTED trailing row predates the exit; order_type is None
        # (off-session) so order_lookup can't classify it.
        log_trailing_stop_action({
            "run_id": "r1", "symbol": "TRL", "action": "CONVERTED", "shares": 10,
            "entry_price": 100.0, "current_price": 108.0, "atr": 2.0,
            "trail_amount": 4.0, "reason": "converted",
            "decided_at": t0 - timedelta(hours=1),
        })
        reconcile_fills(_fetcher(self._round_trip("TRL", 112.0, order_type=None, t=t0)))
        row = _trades("TRL").iloc[0]
        assert row["exit_reason"] == "trailing"

    def test_price_match_default_when_no_signal(self, mem_engine):
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        # No order_type, no trailing log, no bracket on record, no CLOSED_LONG
        # → MKT-or-unknown default to manual_close.
        reconcile_fills(_fetcher(self._round_trip("MAN", 110.0, order_type="MKT", t=t0)))
        assert _trades("MAN").iloc[0]["exit_reason"] == "manual_close"

    def test_signal_flip_when_closed_long_nearby(self, mem_engine):
        from execution.reconciliation import reconcile_fills
        from data.database import log_order_decision

        t0 = _now() - timedelta(days=2)
        exit_t = t0 + timedelta(hours=1)
        log_order_decision({
            "run_id": "r1", "symbol": "FLP", "signal": "SELL",
            "decision": "CLOSED_LONG", "shares": 10, "entry_price": 100.0,
            "stop_price": None, "take_profit_price": None, "position_value": 1000.0,
            "reject_reason": None, "decided_at": exit_t,
        })
        reconcile_fills(_fetcher(self._round_trip("FLP", 110.0, order_type="MKT", t=t0)))
        assert _trades("FLP").iloc[0]["exit_reason"] == "signal_flip"


class TestPriceMatchToleranceRegimes:

    def _seed_bracket(self, symbol, stop, tp, decided_at):
        from data.database import log_order_decision
        log_order_decision({
            "run_id": "r1", "symbol": symbol, "signal": "BUY",
            "decision": "APPROVED", "shares": 10, "entry_price": (stop + tp) / 2,
            "stop_price": stop, "take_profit_price": tp, "position_value": 1000.0,
            "reject_reason": None, "decided_at": decided_at,
        })

    def test_low_price_floor_dominates(self, mem_engine):
        """Low-priced symbol: the $0.05 floor decides the match (0.1% would be tighter)."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        # exit_px=10.04, recorded TP=10.00 → diff 0.04 ≤ max(0.05, 0.0100) = 0.05 ✓
        # 0.1% alone (0.0100) would REJECT 0.04 — so the floor is what matches.
        self._seed_bracket("LOW", stop=8.00, tp=10.00, decided_at=t0 - timedelta(hours=1))
        execs = [
            _exec("EB", "LOW", "BUY",  10, 9.00, t=t0, order_type=None),
            _exec("ES", "LOW", "SELL", 10, 10.04, t=t0 + timedelta(hours=1),
                  order_type=None),
        ]
        reconcile_fills(_fetcher(execs))
        row = _trades("LOW").iloc[0]
        assert row["exit_reason"] == "tp"

    def test_high_price_pct_dominates(self, mem_engine):
        """High-priced symbol: the 0.1% term decides (the $0.05 floor would be too tight)."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        # exit_px=2000.50, recorded stop=2000.00 → diff 0.50 ≤ max(0.05, 2.0005) = 2.0005 ✓
        # $0.05 floor alone would REJECT 0.50 — so the 0.1% term is what matches.
        self._seed_bracket("HIGH", stop=2000.00, tp=2400.00,
                           decided_at=t0 - timedelta(hours=1))
        execs = [
            _exec("EB", "HIGH", "BUY",  10, 2200.0, t=t0, order_type=None),
            _exec("ES", "HIGH", "SELL", 10, 2000.50, t=t0 + timedelta(hours=1),
                  order_type=None),
        ]
        reconcile_fills(_fetcher(execs))
        row = _trades("HIGH").iloc[0]
        assert row["exit_reason"] == "stop"


class TestGapThroughExitReason:
    """Directional gap-aware price-match (2026-06-02 MRVL fix).

    A long's TP (sell LMT) fills at-or-above the TP on a gap-up open; a long's
    stop fills at-or-below the stop on a gap-down.  Before the fix, the symmetric
    tight band classified these as manual_close — losing the exit reason on
    exactly the off-session fills Phase B exists to capture.
    """

    def _seed_bracket(self, symbol, stop, tp, decided_at):
        from data.database import log_order_decision
        log_order_decision({
            "run_id": "r1", "symbol": symbol, "signal": "BUY",
            "decision": "APPROVED", "shares": 10, "entry_price": (stop + tp) / 2,
            "stop_price": stop, "take_profit_price": tp, "position_value": 1000.0,
            "reject_reason": None, "decided_at": decided_at,
        })

    def test_gap_up_fill_above_tp_classified_tp(self, mem_engine):
        """MRVL case: exit $255.90 gapped through TP $244.89 → tp, not manual_close."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        self._seed_bracket("MRVL", stop=183.74, tp=244.89,
                           decided_at=t0 - timedelta(hours=1))
        execs = [
            _exec("EB", "MRVL", "BUY",  10, 205.09, t=t0, order_type=None),
            _exec("ES", "MRVL", "SELL", 10, 255.90, t=t0 + timedelta(hours=1),
                  order_type=None),
        ]
        reconcile_fills(_fetcher(execs))
        assert _trades("MRVL").iloc[0]["exit_reason"] == "tp"

    def test_gap_down_fill_below_stop_classified_stop(self, mem_engine):
        """A gap-down exit well below the recorded stop → stop, not manual_close."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        self._seed_bracket("GAP", stop=90.00, tp=130.00,
                           decided_at=t0 - timedelta(hours=1))
        execs = [
            _exec("EB", "GAP", "BUY",  10, 100.0, t=t0, order_type=None),
            _exec("ES", "GAP", "SELL", 10, 82.50, t=t0 + timedelta(hours=1),
                  order_type=None),
        ]
        reconcile_fills(_fetcher(execs))
        assert _trades("GAP").iloc[0]["exit_reason"] == "stop"

    def test_mid_bracket_price_falls_through_to_default(self, mem_engine):
        """A price between stop and TP matches neither side → default (manual_close)."""
        from execution.reconciliation import reconcile_fills

        t0 = _now() - timedelta(days=2)
        self._seed_bracket("MID", stop=90.00, tp=130.00,
                           decided_at=t0 - timedelta(hours=1))
        execs = [
            _exec("EB", "MID", "BUY",  10, 100.0, t=t0, order_type="MKT"),
            _exec("ES", "MID", "SELL", 10, 110.0, t=t0 + timedelta(hours=1),
                  order_type="MKT"),
        ]
        reconcile_fills(_fetcher(execs))
        assert _trades("MID").iloc[0]["exit_reason"] == "manual_close"


class TestToNaiveUtc:

    def test_tz_aware_utc_coerced(self):
        from execution.reconciliation import _to_naive_utc
        aware = datetime(2026, 5, 27, 13, 46, 55, tzinfo=timezone.utc)
        out = _to_naive_utc(aware)
        assert out.tzinfo is None
        assert out == datetime(2026, 5, 27, 13, 46, 55)

    def test_tz_aware_offset_converted_to_utc(self):
        from execution.reconciliation import _to_naive_utc
        eastern = timezone(timedelta(hours=-4))
        aware = datetime(2026, 5, 27, 9, 46, 55, tzinfo=eastern)   # 13:46:55 UTC
        out = _to_naive_utc(aware)
        assert out.tzinfo is None
        assert out == datetime(2026, 5, 27, 13, 46, 55)

    def test_naive_passthrough(self):
        from execution.reconciliation import _to_naive_utc
        naive = datetime(2026, 5, 27, 13, 46, 55)
        assert _to_naive_utc(naive) == naive
