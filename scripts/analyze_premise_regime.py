"""Tier-1 premise check: does the high-volatility-cohort edge survive bear markets?

The `volatility_cohort_edge.md` finding's binding gate is whether the edge holds
out-of-regime, but the live sample (Apr-Jun 2026) contains zero bear data. This
script substitutes HISTORY for waiting: it reconstructs the edge as a pure price
factor over 2014->present, point-in-time, and splits performance by regime.

Method (no model training, all price):
  - Fixed long-history liquid universe (~125 names that traded since 2014).
  - Point-in-time high-vol cohort: top 40% by TRAILING ATR/price, ranked as-of
    each rebalance (no today's-vol lookahead).
  - Signal proxy: trailing 6-month return (the cross-sectional price signal an
    LSTM-on-price largely captures). The SIGN of the Q5-Q1 spread reveals whether
    momentum or reversal works in each regime.
  - Non-overlapping 21-day holds (also fixes the overlapping-window stats issue).
  - Regime split: per-year, and stress (SPY forward 21d < -5%) vs normal.

CAVEATS (load-bearing — see the finding doc):
  * PROXY, not the LSTM. Momentum is a stand-in; Tier 2 (LSTM-only walk-forward)
    is the faithful test. The burden is on showing the LSTM beats this proxy in
    stress.
  * SURVIVORSHIP. yfinance has no delisted names, so blowups (worst exactly in
    the high-vol cohort during stress) are missing -> stress numbers are
    OPTIMISTIC.
  * Fixed universe, US large/mid only; tamer than the actual traded spec names.

Network-dependent (yfinance fetch). Writes
docs/findings/assets/volatility_cohort_premise_regime.png.
Run from anywhere:  python scripts/analyze_premise_regime.py
"""
import os
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "findings" / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

UNIVERSE = """AAPL MSFT GOOGL AMZN META NVDA AMD INTC CSCO ORCL IBM ADBE CRM QCOM
TXN AVGO MU AMAT INTU NFLX DIS CMCSA T VZ TMUS HD NKE SBUX MCD LOW TGT BKNG MAR GM
F PG KO PEP WMT COST CL MO PM JPM BAC WFC C GS MS AXP BLK SCHW USB PNC COF JNJ PFE
MRK ABBV UNH LLY TMO ABT BMY AMGN GILD CVS MDT DHR BIIB REGN VRTX ILMN XOM CVX COP
SLB EOG OXY PSX VLO MPC HAL BA CAT GE HON UNP UPS RTX LMT DE MMM FDX CSX EMR LIN
FCX NEM NUE APD ECL SHW WYNN LVS MGM DAL UAL AAL LUV CLF M KSS TSLA ETN ADI LRCX
KLAC SNPS CDNS PANW NOW WDAY TEAM DDOG ROKU""".split()

LOOK_MOM = 126   # 6-month momentum lookback (bars)
SKIP = 21        # skip most recent month (microstructure)
HOLD = 21        # forward holding window (bars)
VOL_TOP = 0.40   # high-vol cohort = top 40% by trailing ATR/price
STRESS_THRESH = -0.05   # SPY forward-window return below this = "stress" rebalance


def main():
    univ = sorted(set(UNIVERSE))
    print(f"fetching {len(univ)} symbols + SPY from yfinance (2014-)...")
    raw = yf.download(univ + ["SPY"], start="2014-01-01", auto_adjust=True,
                      progress=False, threads=True)
    close, high, low = raw["Close"], raw["High"], raw["Low"]
    close = close.dropna(axis=1, thresh=int(len(close) * 0.6))
    syms = [s for s in close.columns if s != "SPY"]
    print(f"usable symbols (>=60% history): {len(syms)}")

    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(),
                    (low - prev).abs()]).groupby(level=0).max().reindex(close.index)
    atr_pct = (tr.rolling(14).mean() / close)
    vol_proxy = atr_pct.rolling(40).mean()

    idx = close.index
    spy = close["SPY"]
    records = []
    i = LOOK_MOM + 40
    while i + HOLD < len(idx):
        t = idx[i]
        px_now = close.iloc[i]
        mom = close.iloc[i - SKIP] / close.iloc[i - LOOK_MOM] - 1
        df = pd.DataFrame({"mom": mom, "vp": vol_proxy.iloc[i],
                           "fwd": close.iloc[i + HOLD] / px_now - 1}).loc[syms].dropna()
        if len(df) < 30:
            i += HOLD
            continue
        hv = df[df["vp"] >= df["vp"].quantile(1 - VOL_TOP)].copy()
        if len(hv) < 15:
            i += HOLD
            continue
        hv["Q"] = pd.qcut(hv["mom"], 5, labels=[1, 2, 3, 4, 5], duplicates="drop")
        qret = hv.groupby("Q", observed=True)["fwd"].mean()
        spy_fwd = spy.iloc[i + HOLD] / spy.iloc[i] - 1
        records.append(dict(date=t, spy_fwd=spy_fwd,
                            cohort_excess=hv["fwd"].mean() - spy_fwd,
                            ls=qret.get(5, np.nan) - qret.get(1, np.nan), n=len(hv)))
        i += HOLD

    R = pd.DataFrame(records).set_index("date").sort_index()
    R["year"] = R.index.year
    R["stress"] = R["spy_fwd"] < STRESS_THRESH

    print("\n=== By YEAR: cohort pedestal (excess) and score-proxy slope (Q5-Q1), % ===")
    yr = R.groupby("year").agg(n_rebal=("ls", "size"), spy_fwd_avg=("spy_fwd", "mean"),
                               cohort_excess=("cohort_excess", "mean"), Q5_Q1_slope=("ls", "mean"))
    print((yr.assign(spy_fwd_avg=yr.spy_fwd_avg * 100, cohort_excess=yr.cohort_excess * 100,
                     Q5_Q1_slope=yr.Q5_Q1_slope * 100)).to_string(float_format=lambda x: f"{x:7.2f}"))

    print("\n=== STRESS (SPY fwd<-5%) vs NORMAL, % ===")
    sv = R.groupby("stress").agg(n=("ls", "size"), spy_fwd=("spy_fwd", "mean"),
                                 cohort_excess=("cohort_excess", "mean"), Q5_Q1=("ls", "mean"),
                                 slope_hit=("ls", lambda x: (x > 0).mean()))
    print((sv.assign(spy_fwd=sv.spy_fwd * 100, cohort_excess=sv.cohort_excess * 100,
                     Q5_Q1=sv.Q5_Q1 * 100)).to_string(float_format=lambda x: f"{x:7.2f}"))
    print(f"\nOverall slope: mean {R.ls.mean()*100:.2f}%  median {R.ls.median()*100:.2f}%  "
          f"%positive {(R.ls>0).mean()*100:.0f}%   ({len(R)} non-overlapping rebalances)")

    R["ls_cum"] = (1 + R["ls"].fillna(0)).cumprod()
    R["spy_cum"] = (1 + R["spy_fwd"].fillna(0)).cumprod()
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(R.index, R["ls_cum"], color="#26a69a", lw=2,
            label=f"Long-short Q5-Q1 (vol cohort)  x{R['ls_cum'].iloc[-1]:.2f}")
    ax.plot(R.index, R["spy_cum"], color="#42a5f5", lw=1.4, alpha=.8,
            label=f"SPY (non-overlap 21d compounded)  x{R['spy_cum'].iloc[-1]:.2f}")
    for d in R.index[R["stress"]]:
        ax.axvspan(d, d + pd.Timedelta(days=30), color="#ef5350", alpha=.10)
    ax.axhline(1, color="black", lw=.6, alpha=.3)
    ax.set_title("Tier-1 premise: high-vol-cohort momentum long-short through regimes "
                 "(red = SPY-drop windows)")
    ax.set_ylabel("growth of $1")
    ax.legend(loc="upper left")
    ax.grid(alpha=.2)
    fig.tight_layout()
    out = ASSETS / "volatility_cohort_premise_regime.png"
    fig.savefig(out, dpi=110, facecolor="white")
    print("\nchart ->", os.path.relpath(out, ROOT))


if __name__ == "__main__":
    main()
