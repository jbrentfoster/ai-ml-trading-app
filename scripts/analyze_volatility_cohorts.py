"""Volatility-cohort characterization: live + walk-forward (read-only research).

Splits trades into VOLATILE vs STABLE cohorts by per-symbol characteristic
volatility (mean ATR/price over 1d bars, median threshold) and reports win rate,
avg win/loss, profit factor, mean return, and exit-reason mix for each cohort,
on both `source='live'` trades and the deduped walk-forward book. Establishes
that the edge is concentrated in the volatile cohort across two independent
samples.

Supports docs/findings/volatility_cohort_edge.md (§ "The cohort split").
Table-only (no chart).

Run from anywhere:  python scripts/analyze_volatility_cohorts.py
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = "file:" + (ROOT / "db" / "trading.db").as_posix() + "?mode=ro"

# Median per-symbol ATR/price across traded symbols (recomputed + printed below).
VOL_THRESHOLD = 0.0279


def main():
    con = sqlite3.connect(DB, uri=True, timeout=30)

    vol = pd.read_sql("""
        SELECT i.symbol, AVG(i.atr_14/b.close) AS volp
        FROM indicator_snapshots i
        JOIN ohlcv_bars b
          ON b.symbol=i.symbol AND b.interval=i.interval AND b.timestamp=i.timestamp
        WHERE i.interval='1d' AND i.atr_14 IS NOT NULL AND b.close>0
        GROUP BY i.symbol
    """, con).set_index("symbol")["volp"]

    live = pd.read_sql("""
        SELECT symbol, pnl, pnl_pct, entry_ts, exit_ts, exit_reason, benchmark_return_pct
        FROM trade_log WHERE source='live'
    """, con, parse_dates=["entry_ts", "exit_ts"])

    wf_all = pd.read_sql("""
        SELECT symbol, pnl_pct, entry_ts, exit_ts, exit_reason, run_id, recorded_at
        FROM trade_log WHERE source='walk_forward'
    """, con, parse_dates=["entry_ts", "exit_ts", "recorded_at"])
    latest_run = wf_all.sort_values("recorded_at").groupby("symbol")["run_id"].last()
    wf = wf_all[wf_all.apply(lambda r: r["run_id"] == latest_run.get(r["symbol"]), axis=1)].copy()

    for df in (live, wf):
        df["hold_days"] = (df["exit_ts"] - df["entry_ts"]).dt.total_seconds() / 86400

    traded = set(live.symbol) | set(wf.symbol)
    thr = vol.reindex(sorted(traded)).dropna().median()

    def classify(df):
        df = df.copy()
        df["volp"] = df["symbol"].map(vol)
        df["cohort"] = np.where(df["volp"] > thr, "VOLATILE",
                                np.where(df["volp"].notna(), "STABLE", "UNKNOWN"))
        return df

    live = classify(live)
    wf = classify(wf)

    def stats(df, pnl_col="pnl_pct"):
        n = len(df)
        wins = df[df[pnl_col] > 0]
        losses = df[df[pnl_col] <= 0]
        gp, gl = wins[pnl_col].sum(), -losses[pnl_col].sum()
        return dict(
            N=n,
            win_rate=len(wins) / n * 100 if n else np.nan,
            avg_win_pct=wins[pnl_col].mean() * 100 if len(wins) else np.nan,
            avg_loss_pct=losses[pnl_col].mean() * 100 if len(losses) else np.nan,
            mean_ret_pct=df[pnl_col].mean() * 100,
            median_ret_pct=df[pnl_col].median() * 100,
            profit_factor=gp / gl if gl > 0 else np.inf,
            avg_hold_d=df["hold_days"].mean(),
        )

    def show(title, df, pnl_col="pnl_pct"):
        print("\n" + "=" * 64)
        print(f"{title}   (vol threshold atr/price = {thr*100:.2f}%)")
        print("=" * 64)
        rows = []
        for coh in ["VOLATILE", "STABLE"]:
            s = stats(df[df.cohort == coh], pnl_col)
            s["cohort"] = coh
            rows.append(s)
        t = pd.DataFrame(rows).set_index("cohort")
        cols = ["N", "win_rate", "avg_win_pct", "avg_loss_pct", "mean_ret_pct",
                "median_ret_pct", "profit_factor", "avg_hold_d"]
        print(t[cols].to_string(float_format=lambda x: f"{x:8.2f}"))

    show("LIVE  (real trades, return % per trade)", live)
    volp = live[live.cohort == 'VOLATILE'].pnl.sum()
    stabp = live[live.cohort == 'STABLE'].pnl.sum()
    print(f"\n  LIVE dollar P&L by cohort:   VOLATILE ${volp:,.0f}   STABLE ${stabp:,.0f}")
    top4 = live.nlargest(4, "pnl")
    print(f"  LIVE VOLATILE ex-top4-winners: ${volp - top4.pnl.sum():,.0f}  "
          f"(removed: {', '.join(top4.symbol)})")

    show(f"WALK-FORWARD  (deduped latest run/symbol, {len(wf)} trades, return % per trade)", wf)

    print("\n  WF exit-reason mix (% of cohort trades):")
    mix = wf.groupby(["cohort", "exit_reason"]).size().unstack(fill_value=0)
    print((mix.div(mix.sum(axis=1), axis=0) * 100).round(1).to_string())

    print("\n  LIVE exit-reason mix (% of cohort trades):")
    lmix = live.groupby(["cohort", "exit_reason"]).size().unstack(fill_value=0)
    print((lmix.div(lmix.sum(axis=1), axis=0) * 100).round(1).to_string())

    wf_clean = wf[~((wf.hold_days < 1) & (wf.exit_reason == 'fold_end'))]
    print(f"\n  WF cohort mean_ret_% EXCLUDING 0-day fold_end artifacts (n={len(wf_clean)}):")
    g = wf_clean.groupby("cohort")["pnl_pct"].agg(["count", "mean"])
    g["mean"] = g["mean"] * 100
    print(g.to_string(float_format=lambda x: f"{x:.3f}"))


if __name__ == "__main__":
    main()
