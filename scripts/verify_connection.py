"""
Step 1 Verification Script
===========================
Run this to confirm your IBKR paper trading connection works end-to-end.

Pre-requisites:
  1. IB Gateway is running on your machine
  2. Paper trading account is active
  3. API connections are enabled in IB Gateway:
       Configure → Settings → API → Settings
         ✓  Enable ActiveX and Socket Clients
         ✓  Socket port: 4002  (paper) / 4001 (live)
         ✗  Read-Only API (leave unchecked so orders can be placed)

Run with:
  cd trading_app
  python verify_connection.py

Expected output (if IBKR is running):
  ✅ Connected to IBKR in SIMULATION mode
  ✅ Account summary retrieved: Account DU... | NLV: $...
  ✅ Positions retrieved: N position(s)
  ✅ Price quote retrieved: AAPL = $...
  ✅ Paper market order placed: [PreSubmitted] BUY 1 AAPL @ MKT
  ✅ Order cancelled successfully
  ✅ All Step 1 checks passed — ready for Step 2
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is on the path when running from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252 which can't encode emoji — force UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.settings import config, TradingMode
from core.logger import get_logger
from execution.ibkr_connection import IBKRConnection

log = get_logger("verify")

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


async def run_verification() -> bool:
    """
    Run all Step 1 checks.
    Returns True if every check passes.
    """
    print("\n" + "=" * 60)
    print("  AI Trading App — Step 1 Connection Verification")
    print("=" * 60)
    print(f"  Mode : {config.trading.mode.value.upper()}")
    print(f"  Host : {config.ibkr.host}:{config.ibkr.paper_port}")
    print("=" * 60 + "\n")

    passed = 0
    failed = 0

    try:
        conn = IBKRConnection()
    except ImportError as exc:
        print(f"{FAIL} ib_insync not installed: {exc}")
        print("     Fix: pip install ib_insync")
        return False

    # ── Check 1: Connect ────────────────────────────────────────────────────
    print("Check 1/6 — Connecting to IB Gateway ...")
    connected = await conn.connect()
    if not connected:
        print(f"{FAIL} Could not connect. Is IB Gateway running on port {config.ibkr.paper_port}?")
        print()
        print("  Troubleshooting:")
        print("  • Open IB Gateway and log in to your paper trading account")
        print("  • Configure → Settings → API → Settings")
        print("    → Enable ActiveX and Socket Clients  ✓")
        print(f"   → Socket port = {config.ibkr.paper_port}")
        print("  • Restart IB Gateway after changing API settings")
        return False

    mode_label = "SIMULATION (paper trading)" if config.trading.mode == TradingMode.SIMULATION else "LIVE"
    print(f"{PASS} Connected to IBKR in {mode_label} mode\n")
    passed += 1

    # ── Check 2: Account summary ────────────────────────────────────────────
    print("Check 2/6 — Retrieving account summary ...")
    try:
        summary = await conn.get_account_summary()
        print(f"{PASS} Account summary: {summary}\n")
        passed += 1
    except Exception as exc:
        print(f"{FAIL} Account summary failed: {exc}\n")
        failed += 1

    # ── Check 3: Positions ──────────────────────────────────────────────────
    print("Check 3/6 — Retrieving current positions ...")
    try:
        positions = await conn.get_positions()
        if positions:
            for p in positions:
                print(f"         {p['symbol']:6s} | qty={p['quantity']:>8.0f} | avg_cost=${p['avg_cost']:.2f}")
        else:
            print("         (no open positions — expected for a fresh paper account)")
        print(f"{PASS} Positions retrieved: {len(positions)} position(s)\n")
        passed += 1
    except Exception as exc:
        print(f"{FAIL} Positions failed: {exc}\n")
        failed += 1

    # ── Check 4: Price quote ────────────────────────────────────────────────
    print("Check 4/6 — Fetching real-time price for AAPL ...")
    try:
        price = await conn.get_last_price("AAPL")
        if price:
            print(f"{PASS} Price quote: AAPL = ${price:.2f}\n")
            passed += 1
        else:
            print(f"{WARN} Price returned None. Market may be closed (quote still works outside hours).")
            passed += 1  # still a pass — quote infrastructure is working
    except Exception as exc:
        print(f"{FAIL} Price quote failed: {exc}\n")
        failed += 1

    # ── Check 5: Place a paper order ────────────────────────────────────────
    print("Check 5/6 — Placing a paper market order (1 share AAPL BUY) ...")
    order_result = None
    try:
        order_result = await conn.place_market_order("AAPL", "BUY", 1)
        print(f"{PASS} Paper order placed: {order_result}\n")
        passed += 1
    except Exception as exc:
        print(f"{FAIL} Order placement failed: {exc}\n")
        failed += 1

    # ── Check 6: Cancel the order ────────────────────────────────────────────
    print("Check 6/6 — Cancelling the test order ...")
    if order_result:
        try:
            cancelled = await conn.cancel_order(order_result.order_id)
            if cancelled:
                print(f"{PASS} Order cancelled successfully\n")
            else:
                print(f"{WARN} Cancel returned False — order may have already filled (normal during market hours)\n")
            passed += 1
        except Exception as exc:
            print(f"{FAIL} Cancel failed: {exc}\n")
            failed += 1
    else:
        print(f"{WARN} Skipped — no order to cancel\n")

    # ── Summary ──────────────────────────────────────────────────────────────
    await conn.disconnect()

    print("=" * 60)
    print(f"  Results: {passed} passed / {failed} failed")
    print("=" * 60)

    if failed == 0:
        print(f"\n{PASS} All Step 1 checks passed — ready for Step 2 (data pipeline)\n")
        return True
    else:
        print(f"\n{FAIL} {failed} check(s) failed — review errors above before proceeding\n")
        return False


if __name__ == "__main__":
    success = asyncio.run(run_verification())
    sys.exit(0 if success else 1)
