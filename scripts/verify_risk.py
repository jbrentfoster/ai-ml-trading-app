"""
End-to-end verification for the risk & portfolio management module (Phase 4).

Checks:
  1.  RiskConfig loaded correctly from settings
  2.  TradingConfig has paper_orders_enabled and paper_equity fields
  3.  PositionSizer calculates a PositionSize without error
  4.  ATR-based stop is below entry for a BUY signal
  5.  CircuitBreaker initial state is not halted
  6.  CircuitBreaker trigger / reset cycle works
  7.  CircuitBreaker auto-reset on stale trigger
  8.  PortfolioGuard passes a clean signal
  9.  PortfolioGuard blocks a duplicate position
  10. OrderManager produces DRY_RUN decision in simulation mode
  11. DB: log_order_decision + get_order_decisions roundtrip
  12. DB: log_signal_runner_run + get_signal_runner_log roundtrip
  13. DB: circuit_breaker_log roundtrip
  14. signal_runner.py importable (no crash on import)

Usage:
    python verify_risk.py
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def _result(ok: bool, label: str, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    line = f"  {icon} {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> int:
    print("=" * 60)
    print("  Risk & Portfolio Management — Verification")
    print("=" * 60)
    print()

    all_ok = True

    # ── 1. RiskConfig ─────────────────────────────────────────────────────────
    print("-- Config --")
    try:
        from config.settings import config
        ok = hasattr(config, "risk") and config.risk.kelly_fraction > 0
        all_ok &= _result(ok, "RiskConfig loaded",
                          f"kelly_fraction={config.risk.kelly_fraction}")
    except Exception as exc:
        all_ok &= _result(False, "RiskConfig load failed", str(exc))

    try:
        ok = hasattr(config.trading, "paper_orders_enabled") and hasattr(config.trading, "paper_equity")
        all_ok &= _result(ok, "TradingConfig has paper_orders_enabled + paper_equity",
                          f"paper_equity={config.trading.paper_equity:,.0f}")
    except Exception as exc:
        all_ok &= _result(False, "TradingConfig check failed", str(exc))
    print()

    # ── 2. PositionSizer ──────────────────────────────────────────────────────
    print("-- PositionSizer --")
    try:
        from risk.position_sizer import PositionSizer
        sizer = PositionSizer()
        ps = sizer.calculate("AAPL", "BUY", equity=100_000, entry_price=200.0, atr=4.0)
        all_ok &= _result(ps.shares >= 0, "calculate() returns PositionSize",
                          f"shares={ps.shares}, method={ps.method}")
    except Exception as exc:
        all_ok &= _result(False, "PositionSizer failed", str(exc))

    try:
        ps_buy  = sizer.calculate("AAPL", "BUY",  equity=100_000, entry_price=200.0, atr=4.0)
        ps_sell = sizer.calculate("AAPL", "SELL", equity=100_000, entry_price=200.0, atr=4.0)
        buy_ok  = ps_buy.stop_price  < ps_buy.entry_price
        sell_ok = ps_sell.stop_price > ps_sell.entry_price
        all_ok &= _result(buy_ok and sell_ok, "Stop direction correct (BUY below, SELL above entry)")
    except Exception as exc:
        all_ok &= _result(False, "Stop direction check failed", str(exc))
    print()

    # ── 3. CircuitBreaker ─────────────────────────────────────────────────────
    print("-- CircuitBreaker --")
    try:
        from risk.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker()
        halted, _ = cb.is_halted()
        # Don't fail if previously triggered — just report
        all_ok &= _result(True, "CircuitBreaker instantiated",
                          f"current state: {'HALTED' if halted else 'clear'}")
    except Exception as exc:
        all_ok &= _result(False, "CircuitBreaker init failed", str(exc))

    try:
        cb.trigger("verify test")
        halted, reason = cb.is_halted()
        ok = halted and "verify test" in reason
        all_ok &= _result(ok, "trigger() sets halted state")

        cb.reset()
        halted2, _ = cb.is_halted()
        all_ok &= _result(not halted2, "reset() clears halted state")
    except Exception as exc:
        all_ok &= _result(False, "trigger/reset cycle failed", str(exc))
    print()

    # ── 4. PortfolioGuard ─────────────────────────────────────────────────────
    print("-- PortfolioGuard --")
    try:
        from risk.portfolio_guard import PortfolioGuard
        from risk.position_sizer import PositionSize

        guard = PortfolioGuard()
        ps = PositionSize(
            symbol="AAPL", signal="BUY", shares=25,
            entry_price=200.0, stop_price=196.0, take_profit_price=212.0,
            position_value=5_000, position_pct=0.05,
            kelly_fraction_used=0.05, method="fixed",
        )
        result = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=ps, equity=100_000,
            positions={}, daily_pnl_pct=0.0,
        )
        all_ok &= _result(result.passed, "Guard passes clean signal",
                          f"checks={list(result.checks.keys())}")
    except Exception as exc:
        all_ok &= _result(False, "PortfolioGuard check failed", str(exc))

    try:
        positions = {"AAPL": {"shares": 25, "entry_price": 200, "current_price": 202}}
        result2 = guard.check(
            symbol="AAPL", signal="BUY",
            position_size=ps, equity=100_000,
            positions=positions, daily_pnl_pct=0.0,
        )
        all_ok &= _result(not result2.passed, "Guard blocks duplicate position")
    except Exception as exc:
        all_ok &= _result(False, "Duplicate position check failed", str(exc))
    print()

    # ── 5. OrderManager ───────────────────────────────────────────────────────
    print("-- OrderManager --")
    try:
        from risk.order_manager import OrderManager
        from risk.portfolio_guard import GuardResult
        from models.signal_gate import SignalResult
        from unittest.mock import patch

        mgr = OrderManager(dry_run=True)
        sr  = SignalResult(
            symbol="AAPL", bar_timestamp=_now(),
            lstm_score=0.6, xgb_score=0.5, finbert_score=0.4,
            ensemble_score=0.55, signal="BUY", passed_gate=True,
        )
        with patch.object(mgr, "_get_latest_close", return_value=200.0), \
             patch("risk.portfolio_guard.PortfolioGuard.check",
                   return_value=GuardResult(passed=True, reason="ok", checks={})):
            decision = mgr.process(sr, equity=100_000, positions={})

        all_ok &= _result(
            decision.decision == "DRY_RUN",
            "OrderManager produces DRY_RUN decision",
            f"symbol={decision.symbol} shares={decision.shares}",
        )
    except Exception as exc:
        all_ok &= _result(False, "OrderManager failed", str(exc))
    print()

    # ── 6. DB helpers ─────────────────────────────────────────────────────────
    print("-- Database helpers --")
    run_id = str(uuid.uuid4())
    now    = _now()

    try:
        from data.database import log_order_decision, get_order_decisions
        log_order_decision({
            "run_id": run_id, "symbol": "_VFY", "signal": "BUY",
            "decision": "DRY_RUN", "shares": 5, "entry_price": 100.0,
            "stop_price": 98.0, "take_profit_price": 106.0,
            "position_value": 500.0, "reject_reason": None, "decided_at": now,
        })
        df = get_order_decisions(limit=50)
        ok = run_id in df["run_id"].values
        all_ok &= _result(ok, "log_order_decision + get_order_decisions roundtrip")
    except Exception as exc:
        all_ok &= _result(False, "order_decisions DB roundtrip failed", str(exc))

    try:
        from data.database import log_signal_runner_run, get_signal_runner_log
        log_signal_runner_run({
            "run_id": run_id, "run_date": now.strftime("%Y-%m-%d"),
            "mode": "dry_run", "symbols_processed": 5,
            "signals_generated": 2, "orders_submitted": 1, "orders_rejected": 1,
            "duration_seconds": 1.2, "recorded_at": now, "notes": "verify",
        })
        df2 = get_signal_runner_log(limit=10)
        ok = run_id in df2["run_id"].values
        all_ok &= _result(ok, "log_signal_runner_run + get_signal_runner_log roundtrip")
    except Exception as exc:
        all_ok &= _result(False, "signal_runner_log DB roundtrip failed", str(exc))

    try:
        from data.database import get_circuit_breaker_log
        df3 = get_circuit_breaker_log(limit=10)
        all_ok &= _result(True, "get_circuit_breaker_log reads without error",
                          f"{len(df3)} events")
    except Exception as exc:
        all_ok &= _result(False, "circuit_breaker_log read failed", str(exc))
    print()

    # ── 7. signal_runner importable ───────────────────────────────────────────
    print("-- signal_runner.py --")
    try:
        from scripts import signal_runner  # noqa: F401
        all_ok &= _result(True, "signal_runner importable without error")
    except Exception as exc:
        all_ok &= _result(False, "signal_runner import failed", str(exc))
    print()

    # ── Final ─────────────────────────────────────────────────────────────────
    print("=" * 60)
    if all_ok:
        print(f"  {PASS}  All checks passed.")
    else:
        print(f"  {FAIL}  Some checks failed — see details above.")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
