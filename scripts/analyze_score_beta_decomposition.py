"""Beta-decomposition of the ensemble-score edge (read-only research).

Discriminating test for H1 (selection alpha) vs H2 (regime beta) in
docs/findings/volatility_cohort_edge.md. The portfolio sort measured EXCESS vs
SPY (r_stock - r_spy), which removes the market's LEVEL but not beta
AMPLIFICATION. Here we strip beta too: per-symbol SPY beta from daily returns,
then Jensen alpha = r_stock - beta*r_spy over each event's forward window.

Reads, per volatility cohort and horizon:
  - corr(ensemble_score, beta)        — does the score just pick high-beta names?
  - avg beta by score quintile        — does beta rise with the score?
  - Q5-Q1 spread on RAW excess vs on BETA-ADJUSTED excess (Jensen alpha)
If the beta-adjusted Q5-Q1 slope survives and beta is flat across quintiles -> H1.
If the slope collapses and beta rises with quintile -> H2.

Writes docs/findings/assets/volatility_cohort_beta_decomp.png.
Run from anywhere:  python scripts/analyze_score_beta_decomposition.py
"""
import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
DB = "file:" + (ROOT / "db" / "trading.db").as_posix() + "?mode=ro"
ASSETS = ROOT / "docs" / "findings" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

THR = 0.0279
HORIZONS = (5, 21)
MIN_BETA_OBS = 60


def main():
    con = sqlite3.connect(DB, uri=True, timeout=30)

    vol = pd.read_sql("""
        SELECT i.symbol, AVG(i.atr_14/b.close) volp
        FROM indicator_snapshots i JOIN ohlcv_bars b
          ON b.symbol=i.symbol AND b.interval=i.interval AND b.timestamp=i.timestamp
        WHERE i.interval='1d' AND i.atr_14 IS NOT NULL AND b.close>0
        GROUP BY i.symbol
    """, con).set_index("symbol")["volp"]

    bars = pd.read_sql("SELECT symbol,timestamp,close FROM ohlcv_bars WHERE interval='1d'",
                       con, parse_dates=["timestamp"])
    px = bars.pivot_table(index="timestamp", columns="symbol", values="close").sort_index()
    rets = px.pct_change()
    spy = rets["SPY"]
    spy_var = spy.var()
    beta = {}
    for s in rets.columns:
        pair = pd.concat([rets[s], spy], axis=1).dropna()
        if len(pair) >= MIN_BETA_OBS:
            beta[s] = pair.iloc[:, 0].cov(pair.iloc[:, 1]) / spy_var
    beta = pd.Series(beta)

    # per-symbol position arrays for forward returns
    close_arr, ts_arr, pos_map = {}, {}, {}
    for s, g in bars.sort_values(["symbol", "timestamp"]).groupby("symbol"):
        g = g.reset_index(drop=True)
        close_arr[s] = g["close"].to_numpy()
        ts_arr[s] = g["timestamp"].to_numpy()
        pos_map[s] = {t: i for i, t in enumerate(g["timestamp"])}
    spy_by_ts = dict(zip(ts_arr["SPY"], close_arr["SPY"]))
    spy_ts_sorted = ts_arr["SPY"]

    def spy_close_on(ts):
        c = spy_by_ts.get(ts)
        if c is not None:
            return c
        idx = np.searchsorted(spy_ts_sorted, ts, side="right") - 1
        return close_arr["SPY"][idx] if idx >= 0 else None

    sig = pd.read_sql("SELECT symbol,bar_timestamp,ensemble_score FROM signal_log "
                      "WHERE ensemble_score IS NOT NULL", con, parse_dates=["bar_timestamp"])
    sig["volp"] = sig["symbol"].map(vol)
    sig["cohort"] = np.where(sig["volp"] > THR, "VOLATILE",
                             np.where(sig["volp"].notna(), "STABLE", "UNK"))
    sig["beta"] = sig["symbol"].map(beta)

    def fwd(row, N):
        s = row["symbol"]
        pos = pos_map.get(s, {}).get(row["bar_timestamp"])
        if pos is None or pos + N >= len(close_arr[s]):
            return (np.nan, np.nan)
        c0, c1 = close_arr[s][pos], close_arr[s][pos + N]
        sp0, sp1 = spy_close_on(ts_arr[s][pos]), spy_close_on(ts_arr[s][pos + N])
        if not (c0 and sp0 and sp1):
            return (np.nan, np.nan)
        return (c1 / c0 - 1, sp1 / sp0 - 1)

    res_vol = {}
    print("Per-cohort mean beta:  VOLATILE %.2f   STABLE %.2f"
          % (sig[sig.cohort == "VOLATILE"]["beta"].mean(),
             sig[sig.cohort == "STABLE"]["beta"].mean()))
    for N in HORIZONS:
        d = sig.copy()
        fr = d.apply(lambda r: fwd(r, N), axis=1, result_type="expand")
        d["r_stock"], d["r_spy"] = fr[0], fr[1]
        d = d.dropna(subset=["r_stock", "beta"])
        d["raw_xs"] = d["r_stock"] - d["r_spy"]
        d["beta_adj"] = d["r_stock"] - d["beta"] * d["r_spy"]
        print("\n" + "=" * 86)
        print(f"BETA DECOMPOSITION  N={N}   raw excess (r_stk-r_spy) vs Jensen alpha (r_stk-beta*r_spy)")
        print("=" * 86)
        for coh in ["VOLATILE", "STABLE"]:
            sub = d[d.cohort == coh].copy()
            if len(sub) < 25:
                continue
            csb = sub["ensemble_score"].corr(sub["beta"], method="spearman")
            sub["Q"] = pd.qcut(sub["ensemble_score"], 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"],
                               duplicates="drop")
            g = sub.groupby("Q", observed=True)
            tbl = pd.DataFrame({
                "n": g.size(),
                "avg_beta": g["beta"].mean(),
                "raw_xs_%": g["raw_xs"].mean() * 100,
                "beta_adj_%": g["beta_adj"].mean() * 100,
            })
            print(f"\n[{coh}]  corr(score, beta) = {csb:+.3f}   (n={len(sub)})")
            print(tbl.to_string(float_format=lambda x: f"{x:8.2f}"))
            raw_ls = tbl.loc["Q5", "raw_xs_%"] - tbl.loc["Q1", "raw_xs_%"]
            adj_ls = tbl.loc["Q5", "beta_adj_%"] - tbl.loc["Q1", "beta_adj_%"]
            print(f"   Q5-Q1  RAW = {raw_ls:+.2f}%   BETA-ADJUSTED = {adj_ls:+.2f}%   "
                  f"(survival = {adj_ls/raw_ls*100:.0f}% of raw)" if raw_ls else "")
            # OLS: raw_xs ~ beta + score  (partial effect of score holding beta)
            X = np.column_stack([np.ones(len(sub)), sub["beta"].values,
                                 sub["ensemble_score"].values])
            coef, *_ = np.linalg.lstsq(X, sub["raw_xs"].values, rcond=None)
            print(f"   OLS raw_xs ~ 1 + beta + score:  beta_coef={coef[1]:+.3f}  "
                  f"score_coef={coef[2]:+.3f}  (score effect on 21-bar excess, beta held fixed)")
            if N == 21 and coh == "VOLATILE":
                res_vol = tbl

    if len(res_vol):
        fig, ax = plt.subplots(figsize=(9, 5.5))
        qs = ["Q1", "Q2", "Q3", "Q4", "Q5"]
        ax.plot(qs, [res_vol.loc[q, "raw_xs_%"] for q in qs], "o-", lw=2,
                color="#888", label="RAW excess vs SPY")
        ax.plot(qs, [res_vol.loc[q, "beta_adj_%"] for q in qs], "o-", lw=2,
                color="#26a69a", label="BETA-ADJUSTED (Jensen alpha)")
        ax.axhline(0, color="black", lw=.6, alpha=.4)
        ax.set_title("VOLATILE cohort, N=21: does the score's slope survive beta-stripping?")
        ax.set_xlabel("ensemble-score quintile (Q1 lowest -> Q5 highest)")
        ax.set_ylabel("avg return (%)")
        ax.legend()
        ax.grid(alpha=.2)
        fig.tight_layout()
        out = ASSETS / "volatility_cohort_beta_decomp.png"
        fig.savefig(out, dpi=110, facecolor="white")
        print("\nchart ->", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
