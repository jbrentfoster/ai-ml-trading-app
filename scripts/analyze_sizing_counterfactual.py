"""Risk-sizing counterfactual for live trades (read-only research).

Re-sizes all `source='live'` trades to fixed-fractional risk (each trade risks
the same dollar amount to its stop) and compares the resulting equity curve to
the actual book and to SPY. Tests the hypothesis that the lumpy actual sizing is
costing risk-adjusted return. Verdict (2026-06): it is NOT — the volatile names
that carry the wide stops also carry the profit, so normalizing hurts.

Supports docs/findings/volatility_cohort_edge.md (§ "Sizing is not the lever").
Writes docs/findings/assets/volatility_cohort_equity_sizing.png.

Run from anywhere:  python scripts/analyze_sizing_counterfactual.py
"""
import sqlite3
import math
import os
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

ROOT = Path(__file__).resolve().parent.parent
DB = "file:" + (ROOT / "db" / "trading.db").as_posix() + "?mode=ro"
ASSETS = ROOT / "docs" / "findings" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

E0 = 1_000_000.0          # starting equity for both curves
CAP = 40_000.0            # observed live max notional per position


def parse(ts):
    return datetime.fromisoformat(ts)


def main():
    con = sqlite3.connect(DB, uri=True, timeout=30)
    cur = con.cursor()

    trades = []
    stop_dists = []
    rows = cur.execute("""
        SELECT id, symbol, shares, entry_px, exit_px, pnl, pnl_pct,
               entry_ts, exit_ts, exit_reason, benchmark_return_pct
        FROM trade_log WHERE source='live'
    """).fetchall()
    for (tid, sym, sh, entry, exit_, pnl, pnlpct, ets, xts, reason, bench) in rows:
        st = cur.execute("""
            SELECT stop_price FROM order_decisions
            WHERE symbol=? AND stop_price IS NOT NULL AND decision IN ('APPROVED','DRY_RUN')
            ORDER BY ABS(julianday(decided_at)-julianday(?)) LIMIT 1
        """, (sym, ets)).fetchone()
        stop = st[0] if st else None
        sd = (entry - stop) / entry if stop and stop < entry else None
        trades.append(dict(id=tid, sym=sym, shares=sh, entry=entry, exit=exit_,
                           pnl=pnl, pnl_pct=pnlpct, entry_ts=parse(ets), exit_ts=parse(xts),
                           reason=reason, bench=bench, stop_dist=sd))
        if sd:
            stop_dists.append(sd)

    med_sd = sorted(stop_dists)[len(stop_dists) // 2]
    for t in trades:
        if t["stop_dist"] is None:
            t["stop_dist"] = med_sd          # fallback for the rare missing-stop trade

    for t in trades:
        t["actual_notional"] = t["shares"] * t["entry"]
        t["actual_risk"] = t["actual_notional"] * t["stop_dist"]

    target_risk = sum(t["actual_risk"] for t in trades) / len(trades)

    for t in trades:
        norm_notional = min(target_risk / t["stop_dist"], CAP)
        t["norm_notional"] = norm_notional
        t["norm_pnl"] = t["pnl_pct"] * norm_notional
        t["bench_pnl_actual"] = (t["bench"] or 0) * t["actual_notional"]

    trades.sort(key=lambda t: t["exit_ts"])

    def curve(key):
        eq, c = E0, []
        for t in trades:
            eq += t[key]
            c.append((t["exit_ts"], eq))
        return c

    c_actual = curve("pnl")
    c_norm = curve("norm_pnl")
    c_bench = curve("bench_pnl_actual")

    first_entry = min(t["entry_ts"] for t in trades)
    last_exit = max(t["exit_ts"] for t in trades)
    spy = cur.execute("""
        SELECT timestamp, close FROM ohlcv_bars
        WHERE symbol='SPY' AND interval='1d' AND timestamp>=? AND timestamp<=?
        ORDER BY timestamp
    """, (first_entry.isoformat(sep=' '), last_exit.isoformat(sep=' '))).fetchall()
    spy = [(parse(ts), px) for ts, px in spy]
    spy0 = spy[0][1]
    spy_curve = [(ts, E0 * px / spy0) for ts, px in spy]

    def max_dd(c):
        peak, mdd = -1e18, 0
        for _, v in c:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak)
        return mdd

    def per_trade_sharpe(key):
        rs = [t[key] / E0 for t in trades]
        m = sum(rs) / len(rs)
        sd = (sum((r - m) ** 2 for r in rs) / (len(rs) - 1)) ** 0.5
        span_yrs = (last_exit - first_entry).days / 365.25
        tpy = len(trades) / span_yrs
        return ((m / sd) * math.sqrt(tpy) if sd > 0 else float('nan')), tpy

    def spy_daily_sharpe():
        rets = [(spy[i][1] / spy[i - 1][1] - 1) for i in range(1, len(spy))]
        m = sum(rets) / len(rets)
        sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        return (m / sd) * math.sqrt(252) if sd > 0 else float('nan')

    sh_actual, tpy = per_trade_sharpe("pnl")
    sh_norm, _ = per_trade_sharpe("norm_pnl")
    sh_bench, _ = per_trade_sharpe("bench_pnl_actual")

    def total(c):
        return c[-1][1] - E0

    def ret_pct(c):
        return (c[-1][1] / E0 - 1) * 100

    worst_actual = min(t["pnl"] for t in trades)
    worst_norm = min(t["norm_pnl"] for t in trades)
    mean_a = sum(t["pnl"] for t in trades) / len(trades)
    mean_n = sum(t["norm_pnl"] for t in trades) / len(trades)
    std_actual = (sum((t["pnl"] - mean_a) ** 2 for t in trades) / (len(trades) - 1)) ** 0.5
    std_norm = (sum((t["norm_pnl"] - mean_n) ** 2 for t in trades) / (len(trades) - 1)) ** 0.5

    print("=" * 70)
    print(f"Window: {first_entry.date()} -> {last_exit.date()}   "
          f"({(last_exit - first_entry).days} days, {tpy:.1f} trades/yr)")
    print(f"Target risk/trade (= mean of actual): ${target_risk:,.0f}   notional cap ${CAP:,.0f}")
    print("=" * 70)
    hdr = f"{'metric':<22}{'ACTUAL':>15}{'RISK-NORM':>15}{'SPY-matched':>15}"
    print(hdr)
    print("-" * len(hdr))
    print(f"{'Total P&L':<22}{total(c_actual):>15,.0f}{total(c_norm):>15,.0f}{total(c_bench):>15,.0f}")
    print(f"{'Total return %':<22}{ret_pct(c_actual):>15.2f}{ret_pct(c_norm):>15.2f}{ret_pct(c_bench):>15.2f}")
    print(f"{'Sharpe (per-trade)':<22}{sh_actual:>15.2f}{sh_norm:>15.2f}{sh_bench:>15.2f}")
    print(f"{'Max drawdown %':<22}{max_dd(c_actual)*100:>15.2f}{max_dd(c_norm)*100:>15.2f}{max_dd(c_bench)*100:>15.2f}")
    print(f"{'Worst single trade $':<22}{worst_actual:>15,.0f}{worst_norm:>15,.0f}{'-':>15}")
    print(f"{'Std of trade P&L $':<22}{std_actual:>15,.0f}{std_norm:>15,.0f}{'-':>15}")
    print("-" * len(hdr))
    print(f"\nSPY buy & hold $1M over window: return {ret_pct(spy_curve):.2f}%  "
          f"maxDD {max_dd(spy_curve)*100:.2f}%  dailySharpe {spy_daily_sharpe():.2f}")
    print(f"Aggregate matched excess (sum pnl_pct - bench_pct): "
          f"{sum((t['pnl_pct'] - (t['bench'] or 0)) for t in trades)*100:.1f} pp")

    fig, ax = plt.subplots(figsize=(12, 6.5))

    def xy(c):
        return [d for d, _ in c], [v for _, v in c]

    ax.step(*xy(c_actual), where='post', color="#ef5350", lw=2,
            label=f"Actual  (Sharpe {sh_actual:.2f}, DD {max_dd(c_actual)*100:.1f}%)")
    ax.step(*xy(c_norm), where='post', color="#26a69a", lw=2,
            label=f"Risk-normalized  (Sharpe {sh_norm:.2f}, DD {max_dd(c_norm)*100:.1f}%)")
    ax.step(*xy(c_bench), where='post', color="#888", lw=1.5, ls="--",
            label=f"SPY-matched same bets  (Sharpe {sh_bench:.2f})")
    ax.plot(*xy(spy_curve), color="#42a5f5", lw=1.3, alpha=.8,
            label=f"SPY buy & hold $1M  (ret {ret_pct(spy_curve):.1f}%)")
    ax.axhline(E0, color="black", lw=.6, alpha=.3)
    ax.set_title("Live trades: Actual vs Risk-Normalized sizing vs SPY  (start $1.00M)")
    ax.set_ylabel("Equity ($)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"${v/1e6:.3f}M"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=.2)
    fig.tight_layout()
    out = ASSETS / "volatility_cohort_equity_sizing.png"
    fig.savefig(out, dpi=110, facecolor="white")
    print("\nchart ->", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
