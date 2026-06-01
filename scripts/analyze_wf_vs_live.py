"""
Walk-forward Sharpe vs. realised live-trade outcome correlation.

Answers the question: "does a symbol's walk-forward validation result predict
how its live trades actually turn out?"  See docs/findings/wf_vs_live_correlation.md
for the diagnosis, baseline pins, and hypotheses.

Methodology (point-in-time — this is load-bearing):
  For each closed `source='live'` trade, find the most recent walk_forward_results
  run for that symbol that was recorded *before* the trade's entry_ts, and average
  its per-fold sharpe_ratio.  Matching against the *latest* WF run instead (the
  naive approach) leaks hindsight — a run recorded weeks after the trade — and
  collapses the correlation to ~0.  Always bound the WF lookup by entry_ts.

Usage:
    python scripts/analyze_wf_vs_live.py
    python scripts/analyze_wf_vs_live.py --db db/trading.db

Re-run trigger: whenever Phase B accumulates materially more live round trips
(see the finding's "Trigger to revisit" — next checkpoint is n>=40, hard
re-pin at n>=50).  The numbers printed here are what the finding's status log
should be updated against.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd


def _load(db_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        live = pd.read_sql(
            "SELECT symbol, pnl, pnl_pct, exit_reason, entry_ts, exit_ts "
            "FROM trade_log WHERE source='live'",
            con,
        )
        wf = pd.read_sql(
            "SELECT run_id, symbol, fold_index, sharpe_ratio, recorded_at "
            "FROM walk_forward_results",
            con,
        )
    finally:
        con.close()

    live["entry_ts"] = pd.to_datetime(live["entry_ts"])
    wf["recorded_at"] = pd.to_datetime(wf["recorded_at"])
    return live, wf


def build_matched(live: pd.DataFrame, wf: pd.DataFrame) -> pd.DataFrame:
    """Point-in-time join: each live trade -> mean fold Sharpe of the latest
    WF run for that symbol recorded strictly before the trade's entry_ts."""
    run_agg = (
        wf.groupby(["symbol", "run_id"])
        .agg(sharpe=("sharpe_ratio", "mean"), recorded_at=("recorded_at", "max"))
        .reset_index()
    )

    rows = []
    for _, t in live.iterrows():
        cand = run_agg[
            (run_agg.symbol == t.symbol) & (run_agg.recorded_at <= t.entry_ts)
        ]
        sharpe = (
            cand.sort_values("recorded_at").iloc[-1].sharpe if len(cand) else np.nan
        )
        run_date = (
            cand.sort_values("recorded_at").iloc[-1].recorded_at
            if len(cand)
            else pd.NaT
        )
        rows.append(
            (t.symbol, t.pnl, t.pnl_pct, t.exit_reason, sharpe, run_date)
        )

    m = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "pnl",
            "pnl_pct",
            "exit_reason",
            "wf_sharpe_pit",
            "wf_run_date",
        ],
    )
    m["live_win"] = (m.pnl > 0).astype(int)
    return m.sort_values("wf_sharpe_pit")


def _significance(r: float, n: int) -> str:
    """Two-sided t-test p-value for a Pearson correlation, no scipy dependency."""
    if n < 3 or not np.isfinite(r) or abs(r) >= 1.0:
        return "n/a"
    from math import sqrt

    t = r * sqrt(n - 2) / sqrt(1 - r * r)
    # Normal-approx survival is rough at small n; report t and a coarse verdict.
    verdict = "significant (p<0.05)" if abs(t) > 2.08 else "NOT significant (p>0.05)"
    return f"t={t:.2f}, {verdict}"


def report(m: pd.DataFrame) -> None:
    pd.set_option("display.width", 200)
    valid = m.dropna(subset=["wf_sharpe_pit"])
    n = len(valid)

    print("=== Point-in-time: WF Sharpe known BEFORE each trade entry ===")
    print(m.to_string(index=False))
    print()
    print(f"matched (had a prior WF run): {n} of {len(m)}")
    if n < 3:
        print("Too few matched trades for correlation.")
        return

    r_p = valid.wf_sharpe_pit.corr(valid.pnl_pct)
    r_s = valid.wf_sharpe_pit.corr(valid.pnl_pct, method="spearman")
    print(f"Pearson  corr (WF Sharpe vs live pnl_pct): {r_p:+.3f}  [{_significance(r_p, n)}]")
    print(f"Spearman corr (rank):                      {r_s:+.3f}")

    print()
    print("=== Live outcome split by sign of pre-trade WF Sharpe ===")
    v = valid.copy()
    v["bucket"] = np.where(v.wf_sharpe_pit > 0, "WF > 0", "WF <= 0")
    print(
        v.groupby("bucket")
        .agg(
            n=("pnl", "size"),
            live_winrate=("live_win", "mean"),
            mean_pnl_pct=("pnl_pct", "mean"),
            total_pnl=("pnl", "sum"),
        )
        .to_string()
    )

    print()
    print("=== Robustness: winners had negative WF Sharpe? ===")
    winners_neg = valid[(valid.live_win == 1) & (valid.wf_sharpe_pit < 0)]
    print(
        f"{len(winners_neg)} of {valid.live_win.sum()} live winners had a NEGATIVE "
        f"pre-trade WF Sharpe -> negative WF Sharpe is a poor veto."
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="db/trading.db", help="path to trading.db")
    args = ap.parse_args()

    live, wf = _load(args.db)
    if live.empty:
        print("No source='live' rows in trade_log yet — nothing to analyse.")
        return
    report(build_matched(live, wf))


if __name__ == "__main__":
    main()
