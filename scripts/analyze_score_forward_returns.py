"""Ensemble-score forward-return validation (read-only research).

Unbiased test of whether the ensemble score predicts forward returns: every
`signal_log` score is point-in-time (model trained on prior data), so correlating
it with the symbol's SUBSEQUENT N-bar return has no walk-forward bracket-simulator
or fold-end artifact. Two views, both split by volatility cohort (VOLATILE vs
STABLE) at horizons N=5 and N=21:

  1. Information Coefficient — Spearman rank-corr of score vs forward return
     (overall + per component model), plus directional hit rate.
  2. Portfolio sort — quintile by score, equal-weight, measure EXCESS return vs
     SPY over the same window (strips market level; isolates whether the score
     selects market-beaters beyond just "be in volatile names").

Finding: real monthly-horizon ranking signal, concentrated in volatile names,
sitting on a large regime-beta pedestal (see the finding doc for the beta caveat).

Supports docs/findings/volatility_cohort_edge.md (§ "The score has monthly-
horizon ranking power"). Writes docs/findings/assets/volatility_cohort_score_quintiles.png.

Run from anywhere:  python scripts/analyze_score_forward_returns.py
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

THR = 0.0279          # per-symbol ATR/price median split (see analyze_volatility_cohorts.py)
HORIZONS = (5, 21)


def load(con):
    vol = pd.read_sql("""
        SELECT i.symbol, AVG(i.atr_14/b.close) volp
        FROM indicator_snapshots i JOIN ohlcv_bars b
          ON b.symbol=i.symbol AND b.interval=i.interval AND b.timestamp=i.timestamp
        WHERE i.interval='1d' AND i.atr_14 IS NOT NULL AND b.close>0
        GROUP BY i.symbol
    """, con).set_index("symbol")["volp"]

    sig = pd.read_sql("""
        SELECT symbol, bar_timestamp, ensemble_score, lstm_score, xgb_score, finbert_score
        FROM signal_log WHERE ensemble_score IS NOT NULL
    """, con, parse_dates=["bar_timestamp"])

    bars = pd.read_sql("SELECT symbol,timestamp,close FROM ohlcv_bars WHERE interval='1d'",
                       con, parse_dates=["timestamp"]).sort_values(["symbol", "timestamp"])

    close_arr, ts_arr, pos_map = {}, {}, {}
    for s, g in bars.groupby("symbol"):
        g = g.reset_index(drop=True)
        close_arr[s] = g["close"].to_numpy()
        ts_arr[s] = g["timestamp"].to_numpy()
        pos_map[s] = {t: i for i, t in enumerate(g["timestamp"])}

    sig["volp"] = sig["symbol"].map(vol)
    sig["cohort"] = np.where(sig["volp"] > THR, "VOLATILE",
                             np.where(sig["volp"].notna(), "STABLE", "UNK"))
    return sig, close_arr, ts_arr, pos_map


def fwd_ret(row, N, close_arr, pos_map):
    s = row["symbol"]
    pos = pos_map.get(s, {}).get(row["bar_timestamp"])
    if pos is None or pos + N >= len(close_arr[s]):
        return np.nan
    c0 = close_arr[s][pos]
    return (close_arr[s][pos + N] / c0 - 1) if c0 else np.nan


def ic_view(sig, close_arr, pos_map):
    for N in HORIZONS:
        d = sig.copy()
        d["fwd"] = d.apply(lambda r: fwd_ret(r, N, close_arr, pos_map), axis=1)
        d = d.dropna(subset=["fwd"])
        rows = []
        for label, sub in [("ALL", d)] + [(c, d[d.cohort == c]) for c in ["VOLATILE", "STABLE"]]:
            if len(sub) < 10:
                continue
            dd = sub[sub["ensemble_score"].abs() > 0.15]
            hit = (np.sign(dd["ensemble_score"]) == np.sign(dd["fwd"])).mean() if len(dd) else np.nan
            rows.append(dict(
                cohort=label, n=len(sub),
                IC_ens=sub["ensemble_score"].corr(sub["fwd"], method="spearman"),
                IC_lstm=sub["lstm_score"].corr(sub["fwd"], method="spearman"),
                IC_xgb=sub["xgb_score"].corr(sub["fwd"], method="spearman"),
                IC_finbert=sub["finbert_score"].corr(sub["fwd"], method="spearman"),
                dir_hit=hit, n_dir=len(dd)))
        print("=" * 80)
        print(f"FORWARD-RETURN IC  (N={N})   Spearman rank-corr of score vs fwd return")
        print("=" * 80)
        fmt = {c: (lambda x: f"{x:7.3f}") for c in
               ["IC_ens", "IC_lstm", "IC_xgb", "IC_finbert", "dir_hit"]}
        print(pd.DataFrame(rows).to_string(index=False, formatters=fmt))
        print()


def portfolio_sort(sig, close_arr, ts_arr, pos_map):
    spy_by_ts = dict(zip(ts_arr["SPY"], close_arr["SPY"])) if "SPY" in close_arr else {}
    spy_ts_sorted = ts_arr.get("SPY", np.array([]))

    def spy_close_on(ts):
        c = spy_by_ts.get(ts)
        if c is not None:
            return c
        idx = np.searchsorted(spy_ts_sorted, ts, side="right") - 1
        return close_arr["SPY"][idx] if idx >= 0 else None

    def excess(row, N):
        s = row["symbol"]
        pos = pos_map.get(s, {}).get(row["bar_timestamp"])
        if pos is None or pos + N >= len(close_arr[s]):
            return np.nan
        c0, c1 = close_arr[s][pos], close_arr[s][pos + N]
        sp0, sp1 = spy_close_on(ts_arr[s][pos]), spy_close_on(ts_arr[s][pos + N])
        if not (c0 and sp0 and sp1):
            return np.nan
        return (c1 / c0 - 1) - (sp1 / sp0 - 1)

    res21 = {}
    for N in HORIZONS:
        d = sig.copy()
        d["xs"] = d.apply(lambda r: excess(r, N), axis=1)
        d = d.dropna(subset=["xs"])
        print("=" * 82)
        print(f"PORTFOLIO SORT  hold N={N} bars  |  EXCESS return vs SPY (%), equal-weight")
        print("=" * 82)
        for coh in ["VOLATILE", "STABLE", "ALL"]:
            sub = d if coh == "ALL" else d[d.cohort == coh]
            if len(sub) < 25:
                continue
            sub = sub.copy()
            sub["Q"] = pd.qcut(sub["ensemble_score"], 5,
                               labels=["Q1", "Q2", "Q3", "Q4", "Q5"], duplicates="drop")
            g = sub.groupby("Q", observed=True)["xs"]
            tbl = pd.DataFrame({"n": g.size(), "avg_xs_%": g.mean() * 100,
                                "hit>0": g.apply(lambda x: (x > 0).mean())})
            q5, q1 = tbl.loc["Q5", "avg_xs_%"], tbl.loc["Q1", "avg_xs_%"]
            base = sub["xs"].mean() * 100
            print(f"\n[{coh}]  no-selection baseline excess = {base:+.2f}%  (n={len(sub)})")
            print(tbl.to_string(float_format=lambda x: f"{x:7.2f}"))
            print(f"   Q5={q5:+.2f}%  Q1={q1:+.2f}%  LONG-SHORT Q5-Q1={q5-q1:+.2f}%  "
                  f"score-add (Q5-baseline)={q5-base:+.2f}%")
            if N == 21:
                res21[coh] = tbl["avg_xs_%"]
        print()

    fig, ax = plt.subplots(figsize=(9, 5.5))
    qs = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    for coh, col in [("VOLATILE", "#26a69a"), ("STABLE", "#ef5350")]:
        if coh in res21:
            ax.plot(qs, [res21[coh].get(q, np.nan) for q in qs], "o-", lw=2, label=coh, color=col)
    ax.axhline(0, color="black", lw=.6, alpha=.4)
    ax.set_title("21-bar EXCESS return vs SPY by ensemble-score quintile")
    ax.set_xlabel("ensemble-score quintile (Q1 lowest -> Q5 highest)")
    ax.set_ylabel("avg excess vs SPY (%)")
    ax.legend()
    ax.grid(alpha=.2)
    fig.tight_layout()
    out = ASSETS / "volatility_cohort_score_quintiles.png"
    fig.savefig(out, dpi=110, facecolor="white")
    print("chart ->", os.path.relpath(out, ROOT))


def main():
    con = sqlite3.connect(DB, uri=True, timeout=30)
    sig, close_arr, ts_arr, pos_map = load(con)
    ic_view(sig, close_arr, pos_map)
    portfolio_sort(sig, close_arr, ts_arr, pos_map)


if __name__ == "__main__":
    main()
