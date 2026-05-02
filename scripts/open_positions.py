"""
List and (optionally) close open positions on the IBKR paper account.

Default behaviour is read-only — it lists every held position so you can
see what's open.  Pass `--close` plus a selector to flatten positions
with market sell orders.

Usage — listing:
    python open_positions.py                        # list all positions (default)
    python open_positions.py --list                 # explicit list (same as default)
    python open_positions.py --symbol AAPL          # list only AAPL
    python open_positions.py --symbol AAPL MSFT     # multiple symbols

Usage — closing (requires `--close` + one selector):
    python open_positions.py --close --symbol AAPL           # market-sell full AAPL position
    python open_positions.py --close --symbol AAPL --qty 1   # sell exactly 1 share
    python open_positions.py --close --symbol AAPL MSFT      # close multiple symbols in full
    python open_positions.py --close --all                   # close every position (prompts)
    python open_positions.py --close --all --yes             # same, skip confirmation

Notes:
  * `--qty` is only valid with a single `--symbol` (can't split a qty across symbols).
  * Long positions are flattened with a market SELL; short positions with a
    market BUY (cover).  Either way the position ends at zero shares.
  * Each close fills at the next available price.

Pre-requisites:
  * IB Gateway is running and API connections are enabled
  * Paper trading account is active
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.settings import config
from execution.ibkr_connection import IBKRConnection

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def _print_positions(positions: list[dict]) -> None:
    if not positions:
        print("  (no open positions)")
        return
    print(f"Open positions ({len(positions)}):")
    print(f"  {'Symbol':<8} {'Qty':>8}  {'Avg Cost':>10}  {'Market Value':>14}")
    for p in positions:
        print(
            f"  {p['symbol']:<8} {int(p['quantity']):>8}  "
            f"${p['avg_cost']:>9,.2f}  ${p['market_value']:>13,.2f}"
        )


async def run(
    do_close: bool,
    symbols: list[str] | None,
    qty: int | None,
    close_all: bool,
    skip_confirm: bool,
) -> bool:
    print("\n" + "=" * 60)
    print("  Open Positions")
    print("=" * 60)
    print(f"  Mode : {config.trading.mode.value.upper()}")
    print(f"  Host : {config.ibkr.host}:{config.ibkr.paper_port}")
    print("=" * 60 + "\n")

    conn = IBKRConnection()

    print("Connecting to IB Gateway …")
    connected = await conn.connect()
    if not connected:
        print(f"{FAIL} Could not connect. Is IB Gateway running on port {config.ibkr.paper_port}?")
        return False
    print(f"{PASS} Connected\n")

    try:
        positions = await conn.get_positions()
    except Exception as exc:
        print(f"{FAIL} Could not retrieve positions: {exc}")
        await conn.disconnect()
        return False

    # Apply --symbol filter to the display when not closing (list mode only)
    display_positions = positions
    if not do_close and symbols:
        sym_set = {s.upper() for s in symbols}
        display_positions = [p for p in positions if p["symbol"].upper() in sym_set]

    _print_positions(display_positions)
    print()

    if not do_close:
        await conn.disconnect()
        return True

    # ── Close path ────────────────────────────────────────────────────────────
    if not positions:
        await conn.disconnect()
        return True

    pos_map: dict[str, float] = {p["symbol"].upper(): p["quantity"] for p in positions}

    # Resolve the target symbols
    if close_all:
        targets = list(pos_map.keys())
    else:
        targets = [s.upper() for s in (symbols or [])]

    # Validate & resolve closing action per symbol.  Longs flatten with SELL;
    # shorts cover with BUY.  qty (if given) is the absolute share count.
    to_close: list[tuple[str, str, int]] = []   # (symbol, action, qty)
    for sym in targets:
        if sym not in pos_map:
            print(f"{WARN} No open position for {sym} — skipping.")
            continue
        held = int(pos_map[sym])
        if held == 0:
            print(f"{WARN} {sym}: position is flat — skipping.")
            continue

        action = "SELL" if held > 0 else "BUY"
        held_abs = abs(held)
        close_qty = qty if (qty is not None and len(targets) == 1) else held_abs
        if close_qty > held_abs:
            print(f"{WARN} {sym}: requested qty {close_qty} > held {held_abs}. Capping to {held_abs}.")
            close_qty = held_abs
        to_close.append((sym, action, close_qty))

    if not to_close:
        print(f"{FAIL} Nothing to close.")
        await conn.disconnect()
        return False

    # Confirm for --all
    if close_all and not skip_confirm:
        preview = ", ".join(f"{a} {s} x{q}" for s, a, q in to_close)
        resp = input(f"Close ALL {len(to_close)} positions ({preview})? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            await conn.disconnect()
            return False

    # ── Place market orders ───────────────────────────────────────────────────
    print(f"Closing {len(to_close)} position(s) …")
    filled = 0
    for sym, action, close_qty in to_close:
        print(f"  {action} {close_qty} {sym} @ MKT …")
        try:
            order = await conn.place_market_order(sym, action, close_qty)
            print(f"    {PASS} {order}")
            filled += 1
        except Exception as exc:
            print(f"    {FAIL} {sym}: {exc}")

    await conn.disconnect()

    print()
    print("=" * 60)
    print(f"  Close orders sent : {filled}/{len(to_close)}")
    print("  Positions will flatten once the MKT orders fill.")
    print("=" * 60 + "\n")
    return filled == len(to_close)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List and optionally close IBKR paper positions"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List positions (default when --close is not given)",
    )
    parser.add_argument(
        "--close", action="store_true",
        help="Close positions — must be combined with --symbol or --all",
    )
    parser.add_argument(
        "--symbol", nargs="+", metavar="SYM",
        help="Symbols to filter by (list mode) or close (with --close)",
    )
    parser.add_argument(
        "--qty", type=int, default=None, metavar="N",
        help="Shares to sell — only valid with --close and a single --symbol",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="With --close: close every position (prompts for confirmation)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt for --close --all",
    )
    args = parser.parse_args()

    # Validate flag combinations
    if args.close and not (args.symbol or args.all):
        parser.error("--close requires --symbol or --all")
    if not args.close:
        if args.all:
            parser.error("--all is only valid with --close")
        if args.qty is not None:
            parser.error("--qty is only valid with --close")
    if args.qty is not None:
        if args.all or (args.symbol and len(args.symbol) != 1):
            parser.error("--qty requires exactly one --symbol (can't split a qty across symbols)")

    success = asyncio.run(run(
        do_close=args.close,
        symbols=args.symbol,
        qty=args.qty,
        close_all=args.all,
        skip_confirm=args.yes,
    ))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
