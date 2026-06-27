"""LLM news-score forward-return validation (read-only research).

The faithful Python version of scripts/validate_news_scores.sql, the discriminator
for docs/findings/news_attribution_misallocation.md (H1/H2/H3). Tests whether the
8B's composite_score predicts the RESOLVED ticker's forward move, and whether
re-attribution (matched vs reattributed) carries signal FinBERT's feed-tag
attribution cannot.

Events = (attributed_symbol, calendar day, attr-bucket); event_score = mean
composite_score (mirrors data/news_dedup.py). Forward return = resolved ticker's
N-bar return, entry = first bar strictly after the event day (no same-day
lookahead). News is a fast catalyst, so horizons are short (N=1/3/5), not 21.

EXCESS vs SPY is the primary metric: the raw forward return is confounded by
market drift (in a selloff window everything falls, wrecking the directional-hit
metric regardless of the score), so we measure (ticker return - SPY return) over
the same calendar window. Raw IC is shown alongside for transparency.

H1 cross-check: for reattributed articles, compare the directional hit rate on the
RESOLVED ticker vs the FEED symbol (what FinBERT would score), both on excess.
Resolved > feed means re-attribution adds tradeable signal.

Run from anywhere:  python scripts/analyze_news_scores.py
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = "file:" + (ROOT / "db" / "trading.db").as_posix() + "?mode=ro"
HORIZONS = (1, 3, 5)


def main():
    con = sqlite3.connect(DB, uri=True, timeout=30)

    bars = pd.read_sql("SELECT symbol,timestamp,close FROM ohlcv_bars WHERE interval='1d'",
                       con, parse_dates=["timestamp"]).sort_values(["symbol", "timestamp"])
    close_arr, dates_arr, day_pos = {}, {}, {}
    for s, g in bars.groupby("symbol"):
        g = g.reset_index(drop=True)
        close_arr[s] = g["close"].to_numpy()
        dates_arr[s] = np.array([t.date() for t in g["timestamp"]])
        day_pos[s] = {d: i for i, d in enumerate(dates_arr[s])}

    spy_dates = dates_arr.get("SPY", np.array([]))
    spy_close = close_arr.get("SPY", np.array([]))
    spy_pos = day_pos.get("SPY", {})

    def spy_on(d):
        i = spy_pos.get(d)
        if i is not None:
            return spy_close[i]
        j = np.searchsorted(spy_dates, d, side="right") - 1   # nearest prior
        return spy_close[j] if j >= 0 else None

    def fwd_pair(sym, event_day, N):
        """(ticker N-bar return, SPY return over same calendar window).
        Entry = first ticker bar strictly after event_day."""
        dp = day_pos.get(sym)
        if not dp:
            return (np.nan, np.nan)
        future = [d for d in dp if d > event_day]
        if not future:
            return (np.nan, np.nan)
        e = dp[min(future)]
        if e + N >= len(close_arr[sym]):
            return (np.nan, np.nan)
        c0, c1 = close_arr[sym][e], close_arr[sym][e + N]
        if not c0:
            return (np.nan, np.nan)
        tkr = c1 / c0 - 1
        sp0, sp1 = spy_on(dates_arr[sym][e]), spy_on(dates_arr[sym][e + N])
        spy = (sp1 / sp0 - 1) if (sp0 and sp1) else np.nan
        return (tkr, spy)

    def xs(sym, event_day, N):
        tkr, spy = fwd_pair(sym, event_day, N)
        return tkr - spy if (np.isfinite(tkr) and np.isfinite(spy)) else np.nan

    art = pd.read_sql("""
        SELECT symbol AS feed, attributed_symbol AS resolved, composite_score AS score,
               date(published_at) AS day
        FROM llm_news_analysis
        WHERE parse_ok=1 AND attributed_symbol IS NOT NULL AND composite_score IS NOT NULL
    """, con, parse_dates=["day"])
    art["day"] = art["day"].dt.date
    art["bucket"] = np.where(art["resolved"] == art["feed"], "matched", "reattributed")

    ev = (art.groupby(["resolved", "day", "bucket"], as_index=False)
             .agg(score=("score", "mean"), n_reads=("score", "size")))
    print(f"{len(art)} attributed articles -> {len(ev)} events "
          f"({(ev.bucket=='matched').sum()} matched / {(ev.bucket=='reattributed').sum()} reattributed)\n")

    for N in HORIZONS:
        pair = ev.apply(lambda r: fwd_pair(r["resolved"], r["day"], N), axis=1, result_type="expand")
        ev[f"raw{N}"] = pair[0]
        ev[f"xs{N}"] = pair[0] - pair[1]

    print("=" * 92)
    print("EVENT-LEVEL: does the score predict the RESOLVED ticker's EXCESS-vs-SPY move?")
    print("=" * 92)
    rows = []
    for N in HORIZONS:
        for b in ["matched", "reattributed"]:
            sub = ev[ev.bucket == b].dropna(subset=[f"xs{N}"])
            if len(sub) < 10:
                continue
            dd = sub[sub["score"].abs() > 0.15]
            hit = (np.sign(dd["score"]) == np.sign(dd[f"xs{N}"])).mean() if len(dd) else np.nan
            rows.append(dict(N=N, bucket=b, n=len(sub),
                             IC_xs=sub["score"].corr(sub[f"xs{N}"], method="spearman"),
                             IC_raw=sub["score"].corr(sub[f"raw{N}"], method="spearman"),
                             dir_hit_xs=hit, n_dir=len(dd),
                             avg_xs_bull=sub[sub.score > 0.15][f"xs{N}"].mean() * 100,
                             avg_xs_bear=sub[sub.score < -0.15][f"xs{N}"].mean() * 100))
    t = pd.DataFrame(rows)
    fmt = {c: (lambda x: f"{x:6.3f}") for c in ["IC_xs", "IC_raw", "dir_hit_xs"]}
    fmt.update({c: (lambda x: f"{x:6.2f}") for c in ["avg_xs_bull", "avg_xs_bear"]})
    print(t.to_string(index=False, formatters=fmt))
    print("\n  IC_xs = score vs EXCESS-vs-SPY (drift-stripped, primary);  IC_raw = vs raw return.")
    print("  H2 null: IC_xs ~0 / dir_hit_xs ~0.50 -> noise.  H1: reattributed IC_xs ~ matched -> re-attribution carries signal.")

    print("\n" + "=" * 92)
    print("H1 CROSS-CHECK (reattributed articles): EXCESS-vs-SPY hit on RESOLVED vs FEED symbol")
    print("=" * 92)
    re = art[art.bucket == "reattributed"].copy()
    for N in HORIZONS:
        re[f"r{N}"] = re.apply(lambda r: xs(r["resolved"], r["day"], N), axis=1)
        re[f"f{N}"] = re.apply(lambda r: xs(r["feed"], r["day"], N), axis=1)
        d = re[re["score"].abs() > 0.15].dropna(subset=[f"r{N}", f"f{N}"])
        rh = (np.sign(d["score"]) == np.sign(d[f"r{N}"])).mean()
        fh = (np.sign(d["score"]) == np.sign(d[f"f{N}"])).mean()
        ric = d["score"].corr(d[f"r{N}"], method="spearman")
        fic = d["score"].corr(d[f"f{N}"], method="spearman")
        print(f"  N={N} (n={len(d)}): resolved hit {rh:.3f} / IC {ric:+.3f}   "
              f"vs  feed hit {fh:.3f} / IC {fic:+.3f}   "
              f"-> resolved {'BEATS' if rh > fh else 'does NOT beat'} feed")


if __name__ == "__main__":
    main()
