"""
End-to-end verification for the universe selection module.

Checks:
  1. Alpaca API credentials are configured
  2. TradingClient instantiates without error
  3. get_all_assets() returns at least one record
  4. Stage 1 produces a non-empty asset list (fixtures fallback if Alpaca fails)
  5. Stage 2 narrows the list (or keeps fixtures)
  6. SQLite helpers work: upsert + read roundtrip
  7. get_universe_run_log() reads without error

Usage:
    python verify_universe.py

Set ALPACA_API_KEY and ALPACA_SECRET_KEY in your environment or .env file
before running.  Steps 2-5 are skipped (with a warning) if keys are absent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import config  # loads .env

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def _result(ok: bool, label: str, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    line = f"  {icon} {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


def main() -> int:
    print("=" * 60)
    print("  Universe Selection — Verification")
    print("=" * 60)
    print()

    all_ok = True
    has_keys = bool(config.alpaca.api_key and config.alpaca.secret_key)

    # ── 1. API keys ───────────────────────────────────────────────────────────
    print("── Alpaca credentials ──")
    if not _result(has_keys, "ALPACA_API_KEY and ALPACA_SECRET_KEY present"):
        print(f"  {WARN} Alpaca checks will be skipped — set env vars to verify.")
        all_ok = False
    print()

    # ── 2. TradingClient instantiates ─────────────────────────────────────────
    print("── Alpaca TradingClient ──")
    if has_keys:
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(
                api_key=config.alpaca.api_key,
                secret_key=config.alpaca.secret_key,
                paper=True,
            )
            _result(True, "TradingClient instantiated")
        except Exception as exc:
            _result(False, "TradingClient instantiation failed", str(exc))
            all_ok = False
            client = None
    else:
        print(f"  {WARN} Skipped (no keys)")
        client = None

    # ── 3. get_all_assets returns records ─────────────────────────────────────
    if client is not None:
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass
            assets = client.get_all_assets(
                GetAssetsRequest(asset_class=AssetClass.US_EQUITY)
            )
            count = len(list(assets))
            ok = count > 0
            _result(ok, f"get_all_assets() returned records", f"{count:,} assets")
            if not ok:
                all_ok = False
        except Exception as exc:
            _result(False, "get_all_assets() failed", str(exc))
            all_ok = False
    elif not has_keys:
        print(f"  {WARN} Skipped (no keys)")
    print()

    # ── 4. Stage 1 ────────────────────────────────────────────────────────────
    print("── Universe Stage 1 ──")
    stage1_assets = []
    try:
        from data.universe import UniverseSelector
        sel = UniverseSelector()
        if has_keys:
            stage1_assets = sel._stage1_fetch(run_id="verify")
            ok = len(stage1_assets) > 0
            _result(ok, f"Stage 1 produced assets",
                    f"{len(stage1_assets)} (including fixtures)")
            if not ok:
                all_ok = False
        else:
            # Only fixtures will be present
            from data.universe import UniverseSelector
            # Monkeypatch to avoid raising UniverseError for missing keys
            stage1_assets = [
                {"symbol": s, "name": s, "asset_class": "etf",
                 "is_fixture": True, "market_cap": None, "avg_dollar_volume": None,
                 "stage3_score": None, "active": True}
                for s in config.universe.permanent_fixtures
            ]
            _result(True,
                    "Stage 1 fixture list built (Alpaca skipped)",
                    f"{len(stage1_assets)} fixtures")
    except Exception as exc:
        _result(False, "Stage 1 failed", str(exc))
        all_ok = False
    print()

    # ── 5. Stage 2 narrows list ───────────────────────────────────────────────
    print("── Universe Stage 2 (fixture-only fast path) ──")
    if stage1_assets:
        try:
            fixtures_only = [a for a in stage1_assets if a.get("is_fixture")]
            # Stage 2 on fixtures alone: they bypass the filter
            from data.universe import UniverseSelector
            sel = UniverseSelector()
            stage2 = sel._stage2_filter(fixtures_only, run_id="verify")
            ok = len(stage2) == len(fixtures_only)
            _result(ok, "Fixtures bypass Stage 2 filter",
                    f"{len(stage2)}/{len(fixtures_only)} passed")
            if not ok:
                all_ok = False
        except Exception as exc:
            _result(False, "Stage 2 failed", str(exc))
            all_ok = False
    else:
        print(f"  {WARN} Skipped (no Stage-1 assets)")
    print()

    # ── 6. DB upsert + read roundtrip ─────────────────────────────────────────
    print("── Database helpers ──")
    import uuid
    from datetime import timezone
    from datetime import datetime as _dt

    try:
        from data.database import (
            upsert_universe_asset,
            get_universe_assets,
            log_universe_run,
            get_universe_run_log,
        )
        sym = f"_VFY_{uuid.uuid4().hex[:6].upper()}"
        now = _dt.now(timezone.utc).replace(tzinfo=None)

        upsert_universe_asset({
            "symbol":            sym,
            "name":              "Verify Test",
            "asset_class":       "us_equity",
            "is_fixture":        False,
            "stage":             3,
            "market_cap":        1e9,
            "avg_dollar_volume": 5e6,
            "stage3_score":      0.5,
            "active":            True,
            "added_at":          now,
            "last_scored_at":    now,
            "removed_at":        None,
        })
        df = get_universe_assets(active_only=True)
        ok = sym in df["symbol"].values
        _result(ok, "upsert_universe_asset + get_universe_assets roundtrip")
        if not ok:
            all_ok = False

        # Clean up test row
        from data.database import get_engine, UniverseAsset
        from sqlalchemy.orm import Session
        with Session(get_engine()) as session:
            row = session.query(UniverseAsset).filter_by(symbol=sym).first()
            if row:
                session.delete(row)
                session.commit()

    except Exception as exc:
        _result(False, "upsert_universe_asset failed", str(exc))
        all_ok = False

    # ── 7. Run log ────────────────────────────────────────────────────────────
    try:
        from data.database import log_universe_run, get_universe_run_log
        run_id = str(uuid.uuid4())
        now    = _dt.now(timezone.utc).replace(tzinfo=None)
        log_universe_run({
            "run_id":           run_id,
            "run_type":         "verify",
            "stage":            1,
            "symbol_count":     42,
            "duration_seconds": 0.1,
            "recorded_at":      now,
            "notes":            "verification run",
        })
        df = get_universe_run_log(limit=5)
        ok = run_id in df["run_id"].values
        _result(ok, "log_universe_run + get_universe_run_log roundtrip")
        if not ok:
            all_ok = False
    except Exception as exc:
        _result(False, "log_universe_run failed", str(exc))
        all_ok = False

    print()
    print("=" * 60)
    if all_ok:
        print(f"  {PASS}  All checks passed.")
    else:
        print(f"  {FAIL}  Some checks failed — see details above.")
    print("=" * 60)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
