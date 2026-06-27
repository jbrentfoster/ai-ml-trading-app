"""Buffett-style quality + value + safety screen (large-caps).

Surfaces candidate stocks for the ~20% concentrated "satellite" sleeve of the
core-satellite strategy (docs/strategy/risk_premia_harvesting.md §4). Anchored to
the AQR "Buffett's Alpha" decomposition (Frazzini-Kabiller-Pedersen 2018):
Buffett = quality + value + safety + low-beta, held with patience.

Composite (rank-percentile, robust to outliers), quality-weighted per Buffett's
"wonderful business at a fair price" > "fair business at a wonderful price":
  buffett = 0.45*quality + 0.30*safety + 0.25*value
    quality = ROE, profit margin, earnings growth, revenue growth
    safety  = low debt/equity, current ratio, positive free cash flow
    value   = low forward P/E, low EV/EBITDA, low price/book  (fair, not cheap)

The screen is a *starting shortlist*, NOT a buy list. Apply judgment — moat
durability and management quality (the most valuable part) can't be screened.
CAVEAT: financials / utilities / REITs have structurally different balance
sheets, so the safety component mis-scores them; judge those separately.

Live yfinance fundamentals (current snapshot). Large-cap filter: market cap > $10B.
Run from anywhere:  python scripts/buffett_screen.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
import warnings; warnings.filterwarnings("ignore")

UNIVERSE = """AAPL MSFT GOOGL AMZN META NVDA AVGO ORCL CRM ADBE CSCO ACN QCOM TXN
INTU IBM NOW AMAT ADI LRCX KLAC SNPS CDNS NFLX DIS CMCSA TMUS HD MCD NKE LOW SBUX
TJX BKNG ORLY AZO YUM CMG PG KO PEP COST WMT PM MO MDLZ CL KMB GIS HSY KDP STZ JPM
BAC WFC GS MS AXP BLK SPGI SCHW USB PNC CB MMC PGR AON ICE CME TRV V MA JNJ UNH LLY
ABBV MRK TMO ABT DHR PFE AMGN BMY MDT ISRG SYK GILD CI ELV REGN VRTX CAT HON UNP GE
RTX LMT DE UPS ADP MMM ETN EMR ITW CSX NSC GD NOC PH XOM CVX COP SLB EOG MPC PSX LIN
APD SHW ECL NUE NEE DUK SO""".split()
MIN_MCAP = 10e9
FIELDS = ["returnOnEquity", "profitMargins", "earningsGrowth", "revenueGrowth",
          "debtToEquity", "currentRatio", "freeCashflow", "forwardPE",
          "enterpriseToEbitda", "priceToBook", "marketCap", "sector"]


def fetch(tickers):
    rows = []
    for i, t in enumerate(tickers, 1):
        try:
            info = yf.Ticker(t).info
            rows.append({"ticker": t, **{f: info.get(f) for f in FIELDS}})
        except Exception:
            rows.append({"ticker": t})
        if i % 20 == 0:
            print(f"  ...fetched {i}/{len(tickers)}", file=sys.stderr)
    return pd.DataFrame(rows).set_index("ticker")


def pr(s, ascending=True):
    """rank-percentile in [0,1]; ascending=False means lower raw value scores higher."""
    return s.rank(pct=True, ascending=ascending)


def main():
    print(f"Fetching fundamentals for {len(UNIVERSE)} large-caps (yfinance, ~a few min)...",
          file=sys.stderr)
    df = fetch(sorted(set(UNIVERSE)))
    df = df[df["marketCap"].fillna(0) >= MIN_MCAP].copy()

    # QUALITY (higher = better)
    q = pd.concat([pr(df["returnOnEquity"]), pr(df["profitMargins"]),
                   pr(df["earningsGrowth"]), pr(df["revenueGrowth"])], axis=1).mean(axis=1, skipna=True)
    # SAFETY (low debt, high current ratio, positive FCF)
    s = pd.concat([pr(df["debtToEquity"], ascending=False), pr(df["currentRatio"]),
                   (df["freeCashflow"].fillna(-1) > 0).astype(float)], axis=1).mean(axis=1, skipna=True)
    # VALUE (cheaper = better; fair price, not deep value)
    v = pd.concat([pr(df["forwardPE"], ascending=False), pr(df["enterpriseToEbitda"], ascending=False),
                   pr(df["priceToBook"], ascending=False)], axis=1).mean(axis=1, skipna=True)

    df["quality"], df["safety"], df["value"] = q, s, v
    df["buffett"] = 0.45 * q + 0.30 * s + 0.25 * v
    df = df.sort_values("buffett", ascending=False)

    show = df[["sector", "buffett", "quality", "safety", "value",
               "returnOnEquity", "debtToEquity", "forwardPE", "marketCap"]].copy()
    show["marketCap"] = (show["marketCap"] / 1e9).round(0)
    show["returnOnEquity"] = (show["returnOnEquity"] * 100).round(0)
    for c in ["buffett", "quality", "safety", "value"]:
        show[c] = show[c].round(2)
    print(f"\nBuffett-style screen — top 20 of {len(df)} large-caps (current fundamentals):\n")
    print(show.head(20).to_string())
    print("\n  Shortlist, not a buy list — apply moat/management judgment (the part that can't be screened).")
    print("  Financials/utilities: safety score mis-scores them; assess separately.")
    out = Path(__file__).resolve().parent.parent / "db" / "buffett_screen_latest.csv"
    df.to_csv(out)
    print(f"\nfull ranking -> {out}")


if __name__ == "__main__":
    main()
