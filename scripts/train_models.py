"""
Walk-forward model training script.

Trains LSTM + XGBoost ensemble for each symbol via walk-forward cross-validation,
then retrains on the full dataset and saves checkpoints to models/cache/{symbol}/.

Run this after run_pipeline.py (data must exist in the DB) and before
signal_runner.py (which loads the saved checkpoints).

Usage:
    python train_models.py                        # train all watchlist symbols (full mode)
    python train_models.py --symbol AAPL          # single symbol
    python train_models.py --quick                # faster settings (fewer epochs/folds)
    python train_models.py --interval 1h          # use hourly bars
    python train_models.py --force                # retrain even if checkpoints already exist
    python train_models.py --symbol AAPL --quick  # single symbol, quick mode
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config
from core.logger import get_logger

log = get_logger("train_models")


def _models_exist(symbol: str) -> bool:
    base = Path("models/cache") / symbol
    return (base / "lstm.pt").exists() and (base / "xgb.ubj").exists()


def _apply_quick_mode() -> dict:
    """Temporarily lower training settings for speed.  Returns original values."""
    saved = {
        "lstm_epochs":      config.ml.lstm_epochs,
        "xgb_n_estimators": config.ml.xgb_n_estimators,
        "wf_n_splits":      config.ml.wf_n_splits,
        "wf_train_bars":    config.ml.wf_train_bars,
        "wf_test_bars":     config.ml.wf_test_bars,
    }
    config.ml.lstm_epochs      = 5
    config.ml.xgb_n_estimators = 50
    config.ml.wf_n_splits      = 2
    config.ml.wf_train_bars    = 60
    config.ml.wf_test_bars     = 10
    return saved


def _restore_config(saved: dict) -> None:
    for k, v in saved.items():
        setattr(config.ml, k, v)


def train_symbol(symbol: str, interval: str, force: bool, quick: bool) -> bool:
    """
    Train the ensemble for one symbol.
    Returns True on success, False on failure.
    """
    from data.indicators import IndicatorEngine
    from models.walk_forward import MLWalkForwardOrchestrator

    cache_dir = Path("models/cache") / symbol

    if _models_exist(symbol) and not force:
        print(f"  {symbol}: checkpoints already exist — skipping (use --force to retrain)")
        return True

    engine = IndicatorEngine()
    df = engine.run(symbol, interval=interval)

    if df is None or df.empty:
        print(f"  {symbol}: no data in DB — run run_pipeline.py first")
        return False

    min_bars = config.ml.wf_train_bars + config.ml.wf_gap_bars + config.ml.wf_n_splits * config.ml.wf_test_bars
    if len(df) < min_bars:
        print(
            f"  {symbol}: only {len(df)} bars, need {min_bars} for "
            f"{config.ml.wf_n_splits} folds — skipping"
        )
        return False

    t0 = time.monotonic()
    try:
        orch = MLWalkForwardOrchestrator(symbol=symbol)
        results = orch.run(df)

        cache_dir.mkdir(parents=True, exist_ok=True)
        orch.save_models(cache_dir)

        elapsed = time.monotonic() - t0
        n_folds = len(results)

        sharpes = [r.get("sharpe_ratio") for r in results if r.get("sharpe_ratio") is not None]
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else float("nan")

        print(
            f"  {symbol}: {n_folds} folds | avg Sharpe={avg_sharpe:.3f} | "
            f"{elapsed:.0f}s | saved to {cache_dir}"
        )
        return True

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {symbol}: FAILED after {elapsed:.0f}s — {exc}")
        log.error("Training failed for %s: %s", symbol, exc, exc_info=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward model training")
    parser.add_argument(
        "--symbol", default="", metavar="SYM",
        help="Train a single symbol instead of the full list",
    )
    parser.add_argument(
        "--interval", default="1d",
        help="Bar interval to train on (default: 1d)",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Quick mode: 5 LSTM epochs / 50 XGB trees / 2 folds (much faster, less accurate)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Retrain even if saved checkpoints already exist",
    )
    args = parser.parse_args()

    # Symbol list
    if args.symbol:
        symbols = [args.symbol.upper()]
    elif config.universe.enabled:
        try:
            from data.database import get_universe_assets
            df = get_universe_assets(active_only=True)
            symbols = df["symbol"].tolist() if not df.empty else list(config.data.watchlist)
        except Exception:
            symbols = list(config.data.watchlist)
    else:
        symbols = list(config.data.watchlist)

    mode_label = "quick" if args.quick else "full"
    if args.quick:
        saved_cfg = _apply_quick_mode()

    print(f"\n{'='*60}")
    print(f"  Model Training ({mode_label} mode)")
    print(f"{'='*60}")
    print(f"  Interval : {args.interval}")
    print(f"  Symbols  : {len(symbols)}")
    print(f"  Folds    : {config.ml.wf_n_splits}")
    print(f"  Train    : {config.ml.wf_train_bars} bars")
    print(f"  Test     : {config.ml.wf_test_bars} bars / fold")
    print(f"  LSTM     : {config.ml.lstm_epochs} epochs")
    print(f"  XGBoost  : {config.ml.xgb_n_estimators} estimators")
    print()

    t_total = time.monotonic()
    ok_count = 0
    fail_count = 0

    try:
        for symbol in symbols:
            success = train_symbol(symbol, args.interval, args.force, args.quick)
            if success:
                ok_count += 1
            else:
                fail_count += 1
    finally:
        if args.quick:
            _restore_config(saved_cfg)

    elapsed = time.monotonic() - t_total
    print()
    print(f"{'='*60}")
    print(f"  Done in {elapsed:.0f}s — {ok_count} trained, {fail_count} skipped/failed")
    print()
    print("  Next step:")
    print("    python signal_runner.py")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
