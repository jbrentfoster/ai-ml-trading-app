"""
List and (optionally) cancel open orders on the IBKR paper account.

Default behaviour is read-only — it lists every working order so you can
see what's parked.  Pass `--cancel` plus a selector to actually cancel.

Usage — listing:
    python open_orders.py                         # list all open orders (default)
    python open_orders.py --list                  # explicit list (same as default)
    python open_orders.py --symbol ABBV           # list only ABBV legs
    python open_orders.py --symbol ABBV CL        # list multiple symbols

Usage — cancelling (requires `--cancel` + one selector):
    python open_orders.py --cancel --id 52 53 54  # cancel specific order IDs
    python open_orders.py --cancel --symbol ABBV  # cancel every open leg on ABBV
    python open_orders.py --cancel --all          # cancel every open order (prompts)
    python open_orders.py --cancel --all --yes    # same, skip confirmation

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


def _fmt_price(order: dict) -> str:
    otype = order.get("order_type", "")
    lmt   = order.get("limit_price")
    stop  = order.get("stop_price")
    if otype in ("STP", "STP LMT") and stop is not None:
        return f"stop ${stop:,.2f}" + (f" / lmt ${lmt:,.2f}" if lmt else "")
    if lmt is not None:
        return f"${lmt:,.2f}"
    return "MKT"


def _print_orders(orders: list[dict]) -> None:
    if not orders:
        print("  (no open orders)")
        return
    print(f"Open orders ({len(orders)}):")
    print(f"  {'ID':>6}  {'Symbol':<8} {'Side':<5} {'Qty':>6}  {'Type':<8}  {'Price':<22}  Status")
    for o in orders:
        print(
            f"  {o['order_id']:>6}  {o['symbol']:<8} {o['action']:<5} "
            f"{int(o['quantity']):>6}  {o['order_type']:<8}  "
            f"{_fmt_price(o):<22}  {o['status']}"
        )


async def run(
    do_cancel: bool,
    ids: list[int] | None,
    symbols: list[str] | None,
    cancel_all: bool,
    skip_confirm: bool,
) -> bool:
    print("\n" + "=" * 60)
    print("  Open Orders")
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
        orders = await conn.get_open_orders()
    except Exception as exc:
        print(f"{FAIL} Could not retrieve open orders: {exc}")
        await conn.disconnect()
        return False

    # Apply --symbol filter to the display when not cancelling (list mode only)
    display_orders = orders
    if not do_cancel and symbols:
        sym_set = {s.upper() for s in symbols}
        display_orders = [o for o in orders if o["symbol"].upper() in sym_set]

    _print_orders(display_orders)
    print()

    if not do_cancel:
        await conn.disconnect()
        return True

    # ── Cancel path ───────────────────────────────────────────────────────────
    if not orders:
        await conn.disconnect()
        return True

    targets: list[int] = []
    if cancel_all:
        targets = [o["order_id"] for o in orders]
    else:
        if ids:
            targets.extend(ids)
        if symbols:
            sym_set = {s.upper() for s in symbols}
            targets.extend(
                o["order_id"] for o in orders if o["symbol"].upper() in sym_set
            )
        # dedupe while preserving order
        seen: set[int] = set()
        targets = [i for i in targets if not (i in seen or seen.add(i))]

    # Validate IDs exist
    existing_ids = {o["order_id"] for o in orders}
    unknown = [i for i in targets if i not in existing_ids]
    for i in unknown:
        print(f"{WARN} order_id={i} is not in the open-orders list — skipping.")
    targets = [i for i in targets if i in existing_ids]
    if not targets:
        print(f"{WARN} No matching orders to cancel.")
        await conn.disconnect()
        return False

    # Confirm for --all
    if cancel_all and not skip_confirm:
        resp = input(f"Cancel ALL {len(targets)} open orders? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            await conn.disconnect()
            return False

    print(f"Cancelling {len(targets)} order(s): {targets}")
    sent = 0
    for oid in targets:
        try:
            ok = await conn.cancel_order(oid)
            if ok:
                print(f"  {PASS} cancel sent for id={oid}")
                sent += 1
            else:
                print(f"  {FAIL} id={oid} not found")
        except Exception as exc:
            print(f"  {FAIL} id={oid}: {exc}")

    # Give IBKR a moment to ack, then show remaining
    await asyncio.sleep(1.5)
    try:
        remaining = await conn.get_open_orders()
    except Exception:
        remaining = []

    await conn.disconnect()

    print()
    print("=" * 60)
    print(f"  Cancels sent     : {sent}/{len(targets)}")
    print(f"  Remaining open   : {len(remaining)}")
    print("=" * 60 + "\n")
    return sent == len(targets)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List and optionally cancel open IBKR orders"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List open orders (default when --cancel is not given)",
    )
    parser.add_argument(
        "--cancel", action="store_true",
        help="Cancel orders — must be combined with --id, --symbol, or --all",
    )
    parser.add_argument(
        "--id", type=int, nargs="+", metavar="ID",
        help="Order IDs to cancel (with --cancel)",
    )
    parser.add_argument(
        "--symbol", nargs="+", metavar="SYM",
        help="Symbols to filter by (list mode) or cancel (with --cancel)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="With --cancel: cancel every open order (prompts for confirmation)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt for --cancel --all",
    )
    args = parser.parse_args()

    # Validate: --cancel requires a selector
    if args.cancel and not (args.id or args.symbol or args.all):
        parser.error("--cancel requires one of --id, --symbol, or --all")

    # Validate: selectors without --cancel are only meaningful for --symbol (filter)
    if not args.cancel:
        if args.id:
            parser.error("--id is only valid with --cancel")
        if args.all:
            parser.error("--all is only valid with --cancel")

    success = asyncio.run(run(
        do_cancel=args.cancel,
        ids=args.id,
        symbols=args.symbol,
        cancel_all=args.all,
        skip_confirm=args.yes,
    ))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
