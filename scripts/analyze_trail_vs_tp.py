"""Research / measurement: trail-vs-TP capture on live fast-move winners.

Quantifies the "left on the table" question raised by the WDC 2026-06-16 case
study (docs/case_studies/wdc_2026-06.md) and the CLAUDE.md enhancement
"Trailing stop is structurally crowded out by the bracket TP on fast moves".

For every `source='live'` winner in trade_log it computes two symmetric
counterfactuals against ohlcv_bars + indicator_snapshots, so the trail-vs-TP
decision rides on realised data across BOTH sides rather than a single trade:

  * TP exits  — how much MORE a continued 2xATR trail (started at the TP exit
    price) would have captured over the next N trading days.  Positive =
    the fixed TP cap forfeited upside a trail would have caught.  The downside
    is bounded: the worst a same-day-reversal could give back is ~2xATR.
  * Trailing exits — whether the trail beat the fixed TP it replaced (did the
    bracket TP level get touched during the hold, and was the trail exit above
    it?).  Confirms the trail is not "broken" — it captured the move.

Reproducible: re-run as more live exits accumulate (the n>=3-each-side gate).
Throwaway research artifact — not wired into any runner.

Usage:  .venv/Scripts/python scripts/analyze_trail_vs_tp.py [--post-bars N]
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "db" / "trading.db"


def _bars_after(cur, sym, after_date, limit):
    return cur.execute(
        "SELECT timestamp, open, high, low, close FROM ohlcv_bars "
        "WHERE symbol=? AND interval='1d' AND timestamp > ? ORDER BY timestamp LIMIT ?",
        (sym, after_date + " 23:59", limit),
    ).fetchall()


def _bars_between(cur, sym, start_date, end_date):
    return cur.execute(
        "SELECT timestamp, open, high, low, close FROM ohlcv_bars "
        "WHERE symbol=? AND interval='1d' AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        (sym, start_date, end_date + " 23:59"),
    ).fetchall()


def _entry_atr(cur, sym, entry_date):
    row = cur.execute(
        "SELECT atr_14 FROM indicator_snapshots WHERE symbol=? AND interval='1d' "
        "AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
        (sym, entry_date + " 23:59"),
    ).fetchone()
    return row[0] if row else None


def _last_tp_level(cur, sym, before_ts):
    row = cur.execute(
        "SELECT take_profit_price FROM order_decisions WHERE symbol=? AND signal='BUY' "
        "AND decision='APPROVED' AND decided_at <= ? AND take_profit_price > 0 "
        "ORDER BY decided_at DESC LIMIT 1",
        (sym, before_ts),
    ).fetchone()
    return row[0] if row else None


def trail_from(exit_px, atr, mult, post_bars):
    """Simulate continuing to hold from exit_px with a mult*ATR trailing stop.

    Returns (exit_price, n_bars_used, completed) where completed=False means the
    trail never fired within the available window (still holding at window end)."""
    peak = exit_px
    for i, (_ts, _o, hi, lo, cl) in enumerate(post_bars, start=1):
        peak = max(peak, hi)
        stop = peak - mult * atr
        if lo <= stop:
            return stop, i, True
    if post_bars:
        return post_bars[-1][4], len(post_bars), False  # last close, not yet exited
    return exit_px, 0, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-bars", type=int, default=10)
    args = ap.parse_args()
    N = args.post_bars
    TRAIL_MULT = 2.0  # config.risk.trailing_stop_trail_atr default

    c = sqlite3.connect(str(DB))
    cur = c.cursor()
    rows = cur.execute(
        "SELECT symbol, entry_ts, entry_px, exit_ts, exit_px, shares, pnl, pnl_pct, exit_reason "
        "FROM trade_log WHERE source='live' AND exit_reason IN ('tp','trailing','signal_flip') "
        "ORDER BY exit_reason, exit_ts"
    ).fetchall()

    print(f"Trail-vs-TP capture analysis | post-window={N} bars | trail={TRAIL_MULT}xATR\n")
    hdr = (f"{'sym':<6}{'reason':<12}{'real%':>7}{'atr%':>7}{'peak%':>8}"
           f"{'trailCF%':>9}{'trail$':>10}{'win':>5}  note")
    print(hdr)
    print("-" * len(hdr))

    tp_trailcf = []   # incremental % a trail would add beyond each TP exit
    tp_trail_dollars = []
    for sym, ent, entpx, ex, expx, sh, pnl, pnlpct, reason in rows:
        ent_d, ex_d = ent[:10], ex[:10]
        atr = _entry_atr(cur, sym, ent_d)
        atr_pct = (atr / entpx * 100) if atr else None
        post = _bars_after(cur, sym, ex_d, N)
        peak = max((b[2] for b in post), default=None)
        peak_pct = ((peak - expx) / expx * 100) if peak else None

        note = ""
        trail_cf_pct = trail_dollars = None
        if atr and post:
            t_exit, t_n, done = trail_from(expx, atr, TRAIL_MULT, post)
            trail_cf_pct = (t_exit - expx) / expx * 100
            trail_dollars = (t_exit - expx) * sh
            if not done:
                note = f"trail not fired in {t_n} bars (incomplete window)"
        elif not post:
            note = "no post-exit bars yet (just exited)"
        elif not atr:
            note = "no entry ATR"

        if reason == "tp" and trail_cf_pct is not None and post and (note == "" or "incomplete" in note):
            tp_trailcf.append(trail_cf_pct)
            tp_trail_dollars.append(trail_dollars)

        win = ""
        if reason == "trailing":
            tp_lvl = _last_tp_level(cur, sym, ex)
            hold = _bars_between(cur, sym, ent_d, ex_d)
            if tp_lvl and hold:
                tp_hit = any(b[2] >= tp_lvl for b in hold)
                fixed_tp_pct = ((tp_lvl / entpx - 1) * 100) if tp_hit else (pnlpct * 100)
                win = "Y" if (pnlpct * 100) > fixed_tp_pct + 1e-9 else "n"
                if note:
                    note += "; "
                note += f"fixedTP would give {fixed_tp_pct:+.1f}% (tp_hit={tp_hit})"

        def f(x, w, p=1):
            return f"{x:>{w}.{p}f}" if x is not None else f"{'-':>{w}}"
        print(f"{sym:<6}{reason:<12}{pnlpct*100:>7.1f}{f(atr_pct,7)}{f(peak_pct,8)}"
              f"{f(trail_cf_pct,9)}{f(trail_dollars,10,0)}{win:>5}  {note}")

    print()
    if tp_trailcf:
        n = len(tp_trailcf)
        print(f"TP exits with a usable post-window (n={n}):")
        print(f"  mean incremental capture a 2xATR trail would add beyond the TP: "
              f"{sum(tp_trailcf)/n:+.1f}%")
        print(f"  total $ a trail would have added across these TP exits:        "
              f"${sum(tp_trail_dollars):,.0f}")
        print(f"  positive (trail beats TP): {sum(1 for x in tp_trailcf if x > 0)}/{n}  |  "
              f"negative (TP beats trail): {sum(1 for x in tp_trailcf if x <= 0)}/{n}")
    c.close()


if __name__ == "__main__":
    main()
