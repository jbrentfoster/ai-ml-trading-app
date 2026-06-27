"""
Temporary: compare wf_train_bars=120 vs 252 across a few representative symbols.

Tests whether the per-symbol avg Sharpe improvement we saw on AAPL (0.008 -> 0.971)
holds up across:
  - NVDA: trend-y individual stock (fundamentals featured)
  - KO:   defensive individual stock (fundamentals featured)
  - CAT:  cyclical individual stock (fundamentals featured)
  - SPY:  ETF — XGBoost fundamentals are all 0 for ETFs (no info dict),
          so SPY is the clean control. If SPY gains less than the individual
          stocks proportionally, that's evidence the fundamentals lookahead
          is doing some of the work in the longer-window improvement.

Does NOT save model checkpoints, so existing models/cache/<sym>/ files
(currently 120-bar trained from the last weekly run) are preserved.

Writes a single result line per (symbol, train_bars) to stdout.  Also
appends rows to walk_forward_results in the DB as a side effect of
orchestrator.run() — that's intentional, makes the data inspectable
in Page 4 afterwards.

Delete this file after the experiment is settled.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from data.indicators import IndicatorEngine
from models.walk_forward import MLWalkForwardOrchestrator


SYMBOLS = ["NVDA", "KO", "CAT", "SPY"]
TRAIN_BARS_TO_TEST = [120, 252]
INTERVAL = "1d"

_LOG_BUF: list[str] = []


def out(line: str = "") -> None:
    """Print to stdout AND append to the log buffer."""
    print(line)
    _LOG_BUF.append(line)


def run_one(symbol: str, train_bars: int) -> dict:
    """Run a single walk-forward pass; return summary stats."""
    original = config.ml.wf_train_bars
    config.ml.wf_train_bars = train_bars
    try:
        engine = IndicatorEngine()
        df = engine.run(symbol, interval=INTERVAL)

        if df is None or df.empty:
            return {"symbol": symbol, "train_bars": train_bars, "status": "no data"}

        min_bars = (
            config.ml.wf_train_bars
            + config.ml.wf_gap_bars
            + config.ml.wf_n_splits * config.ml.wf_test_bars
        )
        if len(df) < min_bars:
            return {
                "symbol": symbol,
                "train_bars": train_bars,
                "status": f"only {len(df)} bars, need {min_bars}",
            }

        t0 = time.monotonic()
        orch = MLWalkForwardOrchestrator(symbol=symbol)
        results = orch.run(df)
        # DELIBERATELY do not call orch.save_models() — preserves existing cache

        elapsed = time.monotonic() - t0
        sharpes = [r.get("sharpe_ratio") for r in results if r.get("sharpe_ratio") is not None]
        returns = [r.get("total_return") for r in results if r.get("total_return") is not None]
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else float("nan")
        total_return = sum(returns) if returns else float("nan")
        per_fold = [
            {
                "fold": r.get("fold_index"),
                "sharpe": r.get("sharpe_ratio"),
                "return": r.get("total_return"),
            }
            for r in results
        ]

        return {
            "symbol": symbol,
            "train_bars": train_bars,
            "status": "ok",
            "n_folds": len(results),
            "avg_sharpe": avg_sharpe,
            "total_return": total_return,
            "elapsed_s": elapsed,
            "per_fold": per_fold,
        }
    finally:
        config.ml.wf_train_bars = original


def main() -> None:
    started_at = datetime.now()
    out("\n" + "=" * 72)
    out("  wf_train_bars benchmark: 120 vs 252 across NVDA / KO / CAT / SPY")
    out("=" * 72)
    out(f"  Started: {started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    out(f"  Folds: {config.ml.wf_n_splits}   Test bars/fold: {config.ml.wf_test_bars}")
    out(f"  LSTM epochs: {config.ml.lstm_epochs}   XGBoost estimators: {config.ml.xgb_n_estimators}")
    out()

    rows = []
    for symbol in SYMBOLS:
        for train_bars in TRAIN_BARS_TO_TEST:
            out(f"  >>> {symbol} | train_bars={train_bars} ...")
            res = run_one(symbol, train_bars)
            rows.append(res)
            if res["status"] == "ok":
                out(
                    f"      avg Sharpe={res['avg_sharpe']:+.3f} | "
                    f"total return={res['total_return']*100:+.2f}% | "
                    f"{res['elapsed_s']:.0f}s | "
                    f"folds: "
                    + ", ".join(f"#{f['fold']}={f['sharpe']:+.2f}" for f in res["per_fold"])
                )
            else:
                out(f"      SKIPPED: {res['status']}")
            out()

    # Summary table
    out("\n" + "=" * 72)
    out("  Summary")
    out("=" * 72)
    out(f"  {'Symbol':<8} {'Bars':<6} {'avg Sharpe':<12} {'total return':<14} {'time':<8}")
    out(f"  {'-'*8} {'-'*6} {'-'*12} {'-'*14} {'-'*8}")
    for r in rows:
        if r["status"] != "ok":
            out(f"  {r['symbol']:<8} {r['train_bars']:<6} -- {r['status']}")
            continue
        out(
            f"  {r['symbol']:<8} {r['train_bars']:<6} "
            f"{r['avg_sharpe']:+.3f}       "
            f"{r['total_return']*100:+.2f}%         "
            f"{r['elapsed_s']:.0f}s"
        )

    # Pairwise delta
    out()
    out("  Deltas (252 vs 120):")
    out(f"  {'Symbol':<8} {'dSharpe':<12} {'dReturn':<12} {'dTime':<10}")
    out(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*10}")
    by_sym = {}
    for r in rows:
        if r["status"] == "ok":
            by_sym.setdefault(r["symbol"], {})[r["train_bars"]] = r
    for sym in SYMBOLS:
        pair = by_sym.get(sym, {})
        if 120 in pair and 252 in pair:
            a, b = pair[120], pair[252]
            ds = b["avg_sharpe"] - a["avg_sharpe"]
            dr = (b["total_return"] - a["total_return"]) * 100
            dt = b["elapsed_s"] - a["elapsed_s"]
            out(f"  {sym:<8} {ds:+.3f}       {dr:+.2f}%        +{dt:.0f}s")
        else:
            out(f"  {sym:<8} -- incomplete")

    out()
    out("  Interpretation hint:")
    out("    SPY is the fundamentals-free control (ETF -> XGB fundamental features = 0).")
    out("    If SPY's dSharpe is comparable to the individual stocks', the longer-window")
    out("    gain is mostly real.  If SPY gains far less than the stocks, fundamentals")
    out("    lookahead is doing some of the work.")
    out("=" * 72)
    out()

    # Write captured output to a log file under logs/benchmarks/
    log_dir = Path("logs/benchmarks")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"wf_train_bars_{started_at.strftime('%Y%m%d_%H%M%S')}.log"
    log_path.write_text("\n".join(_LOG_BUF), encoding="utf-8")
    print(f"  Results saved to: {log_path}")


if __name__ == "__main__":
    main()
