"""
Unit tests for scripts/intraday_check.py and risk.order_manager.flatten_all_longs.

The script's gateway / IBKR layer is mocked end-to-end so these run without a
live IB Gateway.  DB writes are captured via monkeypatch so tests don't
pollute the project's SQLite file.

Test coverage (8 tests):
  1. price_source uses IBKR get_last_price, not DB
  2. Gateway-down → status='gateway_down', exit code 0
  3. Dry-run + tripped CB → no flatten call
  4. --no-dry-run + tripped CB + paper enabled → flatten called
  5. Top-level exception writes an error row + exits 0
  6. No equity baseline → cb_tripped stays None
  7. flatten_all_longs cancels TRAIL alongside LMT/STP/STP LMT (broader filter)
  8. flatten_all_longs persists decision='CB_FLATTENED' to order_decisions
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow `import scripts.intraday_check`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.intraday_check as intraday_check
from risk.order_manager import flatten_all_longs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _connection_factory(
    *,
    connect_returns: bool = True,
    nlv: float = 100_000.0,
    positions: list[dict] | None = None,
    open_orders: list[dict] | None = None,
    last_prices: dict[str, float] | None = None,
):
    """Return a MagicMock class whose () call returns a configured AsyncMock connection."""
    summary = MagicMock()
    summary.net_liquidation = nlv
    summary.total_cash      = nlv * 0.5
    summary.unrealized_pnl  = 0.0
    summary.realized_pnl    = 0.0

    conn = MagicMock()
    conn.connect              = AsyncMock(return_value=connect_returns)
    conn.disconnect           = AsyncMock(return_value=None)
    conn.get_account_summary  = AsyncMock(return_value=summary)
    conn.get_positions        = AsyncMock(return_value=positions or [])
    conn.get_open_orders      = AsyncMock(return_value=open_orders or [])
    conn.cancel_order         = AsyncMock(return_value=True)
    conn.place_market_order   = AsyncMock(return_value=MagicMock())

    if last_prices is not None:
        async def _last(sym, *_a, **_kw):
            return last_prices.get(sym)
        conn.get_last_price = AsyncMock(side_effect=_last)
    else:
        conn.get_last_price = AsyncMock(return_value=100.0)

    cls = MagicMock(return_value=conn)
    cls.connection = conn   # convenient handle for assertions
    return cls


@pytest.fixture
def captured_rows(monkeypatch):
    """Capture every intraday_run_log row the script would write.

    Patched on scripts.intraday_check.log_intraday_run because the script
    imports the helper into its own namespace at module load.
    """
    rows: list[dict] = []
    monkeypatch.setattr(
        intraday_check, "log_intraday_run", lambda rec: rows.append(rec)
    )
    return rows


@pytest.fixture
def no_baseline(monkeypatch):
    """Default: no prior equity snapshot — CB check short-circuits to None."""
    monkeypatch.setattr(
        intraday_check, "get_equity_snapshot_on_or_before", lambda *_: None
    )


@pytest.fixture(autouse=True)
def _isolate_circuit_breaker(monkeypatch):
    """Per-test in-memory CircuitBreaker state so tests don't pollute the real DB.

    Without this, the first test that trips the breaker (via real
    ``CircuitBreaker.check_loss_limits``) writes a TRIGGERED row to
    ``circuit_breaker_log``, and every subsequent test sees the breaker
    as already halted — which makes the script short-circuit before the
    code path the test is trying to exercise.

    Patches the two DB seams the CircuitBreaker class uses internally:
    ``get_latest_circuit_breaker_event`` (the read) and
    ``log_circuit_breaker_event`` (the write).  Real CircuitBreaker logic
    still runs end-to-end against the in-memory state dict.
    """
    state: dict = {"event": None}

    def _latest() -> dict | None:
        return state["event"]

    def _log(record: dict) -> None:
        state["event"] = record

    monkeypatch.setattr(
        "risk.circuit_breaker.get_latest_circuit_breaker_event", _latest,
    )
    monkeypatch.setattr(
        "risk.circuit_breaker.log_circuit_breaker_event", _log,
    )
    return state


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestIntradayRunner:

    def test_intraday_runner_uses_ibkr_price_not_db(
        self, monkeypatch, captured_rows, no_baseline,
    ):
        """When manage() is invoked, the price_source it receives must call
        get_last_price() on the IBKR connection — NOT read get_bars from the DB.

        The regression this guards: the daily Phase 3.5 reads yesterday's
        close from ohlcv_bars; the intraday runner must override that with
        the live IBKR quote.  If the override breaks, mid-day evaluations
        silently run against stale prices and ratchet detection becomes
        meaningless.
        """
        ibkr_cls = _connection_factory(
            connect_returns=True,
            positions=[{"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0}],
        )
        observed_price_source = {}

        def fake_manage(self, run_id="", *, price_source=None, intraday=False):
            # Capture the callable; invoke it to prove it routes to IBKR.
            observed_price_source["fn"] = price_source
            observed_price_source["intraday"] = intraday
            if price_source is not None:
                observed_price_source["resolved"] = price_source("AAPL")
            return []

        monkeypatch.setattr(
            "execution.ibkr_connection.IBKRConnection", ibkr_cls,
        )
        monkeypatch.setattr(
            "risk.trailing_stop.TrailingStopManager.manage", fake_manage,
        )
        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = True
            ibkr_cls.connection.get_last_price = AsyncMock(return_value=137.42)
            rc = intraday_check.run(dry_run=True)

        assert rc == 0
        assert observed_price_source.get("intraday") is True
        assert observed_price_source.get("resolved") == pytest.approx(137.42)
        # The IBKR async call fired exactly once for the one evaluated symbol.
        ibkr_cls.connection.get_last_price.assert_awaited_once_with("AAPL")
        # Status row was written as completed.
        assert len(captured_rows) == 1
        assert captured_rows[0]["status"] == "completed"

    def test_intraday_runner_gateway_down_exits_clean(
        self, monkeypatch, captured_rows,
    ):
        """connect() returning False → status='gateway_down', exit code 0,
        row persisted before return.  No CB-check work attempted.

        This is the Task-Scheduler retry-storm avoidance contract: a flaky
        Gateway must NOT make the runner exit non-zero, or every missed
        slot would trigger TS to re-run aggressively.
        """
        ibkr_cls = _connection_factory(connect_returns=False)
        monkeypatch.setattr(
            "execution.ibkr_connection.IBKRConnection", ibkr_cls,
        )

        rc = intraday_check.run(dry_run=True)

        assert rc == 0
        assert len(captured_rows) == 1
        row = captured_rows[0]
        assert row["status"] == "gateway_down"
        assert row["daily_loss_pct"] is None
        assert row["weekly_loss_pct"] is None
        assert row["cb_tripped"] is None
        assert row["error_message"] == "IBKR connect failed or raised"

    def test_intraday_runner_dry_run_skips_flatten(
        self, monkeypatch, captured_rows,
    ):
        """Dry-run mode + tripped CB → flatten_all_longs is NOT called.
        The CB row is still written so observability is preserved."""
        ibkr_cls = _connection_factory(connect_returns=True, nlv=90_000.0)
        monkeypatch.setattr(
            "execution.ibkr_connection.IBKRConnection", ibkr_cls,
        )
        # Pretend we have a baseline at $100k → 10% loss → tripped.
        monkeypatch.setattr(
            intraday_check, "get_equity_snapshot_on_or_before",
            lambda d: {"net_liquidation": 100_000.0},
        )
        flatten_mock = MagicMock(return_value=0)
        monkeypatch.setattr(intraday_check, "flatten_all_longs", flatten_mock)

        rc = intraday_check.run(dry_run=True)

        assert rc == 0
        flatten_mock.assert_not_called()
        assert captured_rows[0]["status"] == "cb_tripped"
        assert captured_rows[0]["cb_tripped"] == 1
        assert captured_rows[0]["positions_flattened"] == 0

    def test_intraday_runner_no_dry_run_with_tripped_cb_calls_flatten(
        self, monkeypatch, captured_rows,
    ):
        """--no-dry-run + tripped CB + paper_orders_enabled=True → flatten fires.

        The intraday runner's whole point: a portfolio-level loss between
        scheduled slots should result in real position-flattening, not just
        a log row.  This is the test that pins that contract.
        """
        ibkr_cls = _connection_factory(connect_returns=True, nlv=90_000.0)
        monkeypatch.setattr(
            "execution.ibkr_connection.IBKRConnection", ibkr_cls,
        )
        monkeypatch.setattr(
            intraday_check, "get_equity_snapshot_on_or_before",
            lambda d: {"net_liquidation": 100_000.0},
        )
        flatten_mock = MagicMock(return_value=2)
        monkeypatch.setattr(intraday_check, "flatten_all_longs", flatten_mock)

        with patch("scripts.intraday_check.config") as cfg:
            from config.settings import TradingMode
            cfg.trading.mode = TradingMode.SIMULATION
            cfg.trading.paper_orders_enabled = True
            cfg.risk.trailing_stop_enabled = False
            # Mirror the real CircuitBreaker thresholds the script reads.
            cfg.risk.circuit_breaker_daily_loss_pct = 0.03
            cfg.risk.circuit_breaker_weekly_loss_pct = 0.07
            cfg.risk.circuit_breaker_reset_hours = 24
            rc = intraday_check.run(dry_run=False)

        assert rc == 0
        flatten_mock.assert_called_once()
        # Called positionally (ibkr, loop, run_id=...) — verify the run_id
        # threaded through is the script's UUID, not a literal.
        _, kwargs = flatten_mock.call_args
        assert "run_id" in kwargs and len(kwargs["run_id"]) >= 8
        row = captured_rows[0]
        assert row["status"] == "cb_tripped"
        assert row["positions_flattened"] == 2

    def test_intraday_runner_top_level_exception_writes_error_row(
        self, monkeypatch,
    ):
        """An unhandled exception inside run() must still write an error row
        and return 0.  This is the last-line-of-defense path that protects
        Task Scheduler from retry-storming on truly unexpected failures."""
        # Force IBKRConnection() itself to raise — exercises the _setup_loop_and_connect
        # except branch, which already returns (None, None) → gateway_down.
        # To trigger the OUTER except in main() we monkeypatch run() to raise.
        captured = []
        monkeypatch.setattr(
            intraday_check, "log_intraday_run", lambda rec: captured.append(rec)
        )
        monkeypatch.setattr(
            intraday_check, "run",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # main() calls sys.argv parsing; mock to avoid surprises.
        monkeypatch.setattr(sys, "argv", ["intraday_check.py", "--dry-run"])

        rc = intraday_check.main()

        assert rc == 0
        assert len(captured) == 1
        assert captured[0]["status"] == "error"
        assert "boom" in (captured[0]["error_message"] or "")

    def test_intraday_runner_no_baseline_skips_cb_check(
        self, monkeypatch, captured_rows, no_baseline,
    ):
        """Fresh DB with no prior equity_snapshots row → cb_tripped stays None.
        Phase 3.5 still runs."""
        ibkr_cls = _connection_factory(connect_returns=True, positions=[])
        monkeypatch.setattr(
            "execution.ibkr_connection.IBKRConnection", ibkr_cls,
        )
        with patch("risk.trailing_stop.config") as cfg:
            cfg.risk.trailing_stop_enabled = False  # skip the manager entirely
            rc = intraday_check.run(dry_run=True)

        assert rc == 0
        row = captured_rows[0]
        assert row["status"] == "completed"
        assert row["cb_tripped"] is None
        assert row["daily_loss_pct"] is None
        assert row["weekly_loss_pct"] is None


class TestFlattenAllLongs:
    """Verify the CB-trip flatten helper.  Critical contract: the cancel
    filter must include TRAIL alongside LMT / STP / STP LMT — without it,
    a converted trailing stop survives the flatten and could open a short
    when it later fires against zero shares.  This is the same bug class
    as `_cancel_bracket_children` missing TRAIL (tracked separately for
    its own one-line PR)."""

    def _loop(self):
        """Tiny synchronous loop driver — fine for AsyncMock teardown."""
        import asyncio
        loop = asyncio.new_event_loop()
        return loop

    def test_flatten_all_longs_cancels_trail(self, monkeypatch):
        """A long with an active TRAIL alongside LMT+STP must see all three
        cancelled before the market sell."""
        loop = self._loop()
        try:
            ibkr = MagicMock()
            ibkr.get_positions = AsyncMock(return_value=[
                {"symbol": "AAPL", "quantity": 100, "avg_cost": 100.0},
            ])
            ibkr.get_open_orders = AsyncMock(return_value=[
                {"order_id": 1, "symbol": "AAPL", "action": "SELL",
                 "order_type": "LMT", "limit_price": 110.0},
                {"order_id": 2, "symbol": "AAPL", "action": "SELL",
                 "order_type": "STP", "stop_price": 96.0},
                {"order_id": 3, "symbol": "AAPL", "action": "SELL",
                 "order_type": "TRAIL", "stop_price": 4.0},
            ])
            ibkr.cancel_order = AsyncMock(return_value=True)
            ibkr.place_market_order = AsyncMock(return_value=MagicMock())

            # Don't write to the real DB.
            monkeypatch.setattr(
                "risk.order_manager.log_order_decision", lambda *_a, **_kw: None,
            )

            n = flatten_all_longs(ibkr, loop, run_id="test-run")

            assert n == 1   # one position flattened
            # All three bracket legs cancelled, including TRAIL.
            cancelled_ids = [c.args[0] for c in ibkr.cancel_order.await_args_list]
            assert sorted(cancelled_ids) == [1, 2, 3]
            ibkr.place_market_order.assert_awaited_once_with("AAPL", "SELL", 100)
        finally:
            loop.close()

    def test_flatten_all_longs_persists_cb_flattened(self, monkeypatch):
        """Each successful flatten writes a decision='CB_FLATTENED' row to
        order_decisions so Page 8 / post-mortem can see what the breaker did."""
        loop = self._loop()
        try:
            ibkr = MagicMock()
            ibkr.get_positions = AsyncMock(return_value=[
                {"symbol": "GLD", "quantity": 50, "avg_cost": 200.0},
                {"symbol": "AAPL", "quantity": 100, "avg_cost": 150.0},
            ])
            ibkr.get_open_orders = AsyncMock(return_value=[])
            ibkr.cancel_order = AsyncMock(return_value=True)
            ibkr.place_market_order = AsyncMock(return_value=MagicMock())

            persisted: list[dict] = []
            monkeypatch.setattr(
                "risk.order_manager.log_order_decision",
                lambda rec: persisted.append(rec),
            )

            n = flatten_all_longs(ibkr, loop, run_id="cb-1")

            assert n == 2
            assert len(persisted) == 2
            symbols_logged = {p["symbol"] for p in persisted}
            assert symbols_logged == {"GLD", "AAPL"}
            for p in persisted:
                assert p["decision"] == "CB_FLATTENED"
                assert p["signal"]   == "SELL"
                assert p["run_id"]   == "cb-1"
                assert p["shares"]   > 0
        finally:
            loop.close()
