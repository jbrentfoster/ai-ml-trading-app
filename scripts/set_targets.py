"""Manage the target_allocation table — the rebalancer's source of truth.

  python scripts/set_targets.py --show              # current active targets + checks
  python scripts/set_targets.py --init-core         # set the pinned ETF core
  python scripts/set_targets.py --qv "AAPL:0.05,KO:0.05,JNJ:0.05"   # set quality-value sat
  python scripts/set_targets.py --bigbet "ANTH:0.025"               # set big-bet(s)

Each --qv / --bigbet rewrites *that sleeve's* active rows (the rest are untouched);
pass an empty string to clear a sleeve.  Big-bet caps are enforced HERE, at entry
(<=3% per name, <=5% aggregate) — see docs/strategy/risk_premia_harvesting.md §4/§6.
Run from the project root.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.database import get_target_allocation, replace_target_sleeves
from portfolio.allocation import CORE, SAT_QV, SAT_BIGBET

# Pinned ETF core — docs/strategy/risk_premia_harvesting.md §6
PINNED_CORE = [
    {"ticker": "VLUE", "sleeve": CORE, "target_weight": 0.22, "label": "US value"},
    {"ticker": "QUAL", "sleeve": CORE, "target_weight": 0.22, "label": "US quality"},
    {"ticker": "EFV",  "sleeve": CORE, "target_weight": 0.08, "label": "Intl developed value"},
    {"ticker": "IEF",  "sleeve": CORE, "target_weight": 0.14, "label": "US Treasuries 7-10y"},
    {"ticker": "GLD",  "sleeve": CORE, "target_weight": 0.08, "label": "Gold"},
    {"ticker": "PDBC", "sleeve": CORE, "target_weight": 0.06, "label": "Broad commodities"},
]
BIGBET_NAME_CAP = 0.03
BIGBET_TOTAL_CAP = 0.05
_SLEEVE_ORDER = [CORE, SAT_QV, SAT_BIGBET]
_EXPECTED = {CORE: 0.80, SAT_QV: 0.15, SAT_BIGBET: None}   # None = capped, not a target sum


def _parse_spec(spec: str, sleeve: str) -> list[dict]:
    rows = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        tick, sep, wt = part.partition(":")
        if not sep:
            raise SystemExit(f"bad spec '{part}' — expected TICKER:weight (e.g. AAPL:0.05)")
        rows.append({"ticker": tick.strip().upper(), "sleeve": sleeve,
                     "target_weight": float(wt)})
    return rows


def _bigbet_cap_errors(rows: list[dict]) -> list[str]:
    errs = []
    total = sum(r["target_weight"] for r in rows)
    if total > BIGBET_TOTAL_CAP + 1e-9:
        errs.append(f"aggregate {total*100:.1f}% > {BIGBET_TOTAL_CAP*100:.0f}% cap")
    for r in rows:
        if r["target_weight"] > BIGBET_NAME_CAP + 1e-9:
            errs.append(f"{r['ticker']} {r['target_weight']*100:.1f}% > {BIGBET_NAME_CAP*100:.0f}%/name cap")
    return errs


def _show() -> None:
    rows = get_target_allocation(active_only=True)
    if not rows:
        print("No active targets.  Run --init-core to seed the ETF core.")
        return
    by_sleeve: dict[str, list[dict]] = {}
    for r in rows:
        by_sleeve.setdefault(r["sleeve"], []).append(r)
    total = 0.0
    for sleeve in _SLEEVE_ORDER:
        srows = by_sleeve.get(sleeve, [])
        if not srows:
            continue
        s = sum(r["target_weight"] for r in srows)
        total += s
        exp = _EXPECTED.get(sleeve)
        tag = "" if exp is None else f" (target {exp*100:.0f}%)"
        print(f"\n[{sleeve}]  sum {s*100:.1f}%{tag}")
        for r in sorted(srows, key=lambda x: -x["target_weight"]):
            print(f"  {r['ticker']:6} {r['target_weight']*100:5.1f}%  {r['label'] or ''}")
    print(f"\nTotal active weight: {total*100:.1f}%")
    for e in _bigbet_cap_errors(by_sleeve.get(SAT_BIGBET, [])):
        print(f"  ⚠ big-bet cap breached: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage the target_allocation table.")
    ap.add_argument("--show", action="store_true", help="show current active targets")
    ap.add_argument("--init-core", action="store_true", help="set the pinned ETF core")
    ap.add_argument("--qv", help="replace satellite_qv: 'TICK:wt,TICK:wt' (empty clears it)")
    ap.add_argument("--bigbet", help="replace satellite_bigbet: 'TICK:wt' (caps enforced)")
    args = ap.parse_args()

    if args.init_core:
        n = replace_target_sleeves(PINNED_CORE, {CORE})
        print(f"Set {n} core targets.")
    if args.qv is not None:
        n = replace_target_sleeves(_parse_spec(args.qv, SAT_QV), {SAT_QV})
        print(f"Set {n} quality-value target(s).")
    if args.bigbet is not None:
        rows = _parse_spec(args.bigbet, SAT_BIGBET)
        errs = _bigbet_cap_errors(rows)
        if errs:
            print("REFUSED — big-bet caps exceeded (sized so a total loss is fine; see docs/strategy §4):")
            for e in errs:
                print("  -", e)
            sys.exit(1)
        n = replace_target_sleeves(rows, {SAT_BIGBET})
        print(f"Set {n} big-bet target(s).")

    _show()


if __name__ == "__main__":
    main()
