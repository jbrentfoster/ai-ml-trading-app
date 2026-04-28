"""
Automated stock universe selector.

Three-stage funnel:
  Stage 1 — Alpaca assets endpoint: all active, tradable US equities
             (up to config.universe.stage1_max)
  Stage 2 — Liquidity / market-cap filter:
             market_cap >= min_market_cap AND
             avg_daily_dollar_volume >= min_avg_dollar_volume
  Stage 3 — XGBoost ranking (or market-cap ranking if no model):
             keep top stage3_max candidates

Permanent fixtures (SPY, QQQ, sector ETFs, etc.) bypass every filter and
are always included in the active list.

Results are persisted to universe_assets and universe_run_log in SQLite.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd

import yfinance as yf

from config.settings import config
from core.logger import get_logger
from data.database import (
    get_bars,
    get_universe_assets,
    log_universe_run,
    upsert_universe_asset,
)
from data.fetcher import DataFetcher
from data.fundamentals import FundamentalsClient
from data.indicators import IndicatorEngine

if TYPE_CHECKING:
    from models.xgboost_model import XGBoostModel

log = get_logger("data.universe")


class UniverseError(Exception):
    """Raised when a whole stage fails (not a per-symbol error)."""


@dataclass
class UniverseRunResult:
    run_id: str
    run_type: str                          # "full" | "rescore"
    stage1_count: int = 0
    stage2_count: int = 0
    stage3_count: int = 0
    candidate_symbols: list = field(default_factory=list)
    duration_seconds: float = 0.0


class UniverseSelector:
    """
    Drives the three-stage universe selection funnel.

    Parameters
    ----------
    xgb_model:
        Optional trained XGBoostModel instance.  When provided it is used to
        score symbols in Stage 3.  When None, Stage 3 falls back to sorting
        by market cap descending.
    """

    def __init__(self, xgb_model: XGBoostModel | None = None) -> None:
        self._cfg = config.universe
        self._xgb = xgb_model

    # ── Public API ────────────────────────────────────────────────────────────

    def run_full(self) -> UniverseRunResult:
        """Execute all three stages and persist results."""
        run_id   = str(uuid.uuid4())
        t_start  = time.monotonic()
        result   = UniverseRunResult(run_id=run_id, run_type="full")

        log.info("[universe] Starting full refresh  run_id=%s", run_id)

        # Stage 1
        assets = self._stage1_fetch(run_id)
        result.stage1_count = len(assets)

        # Stage 2
        assets = self._stage2_filter(assets, run_id)
        result.stage2_count = len(assets)

        # Stage 3
        assets = self._stage3_score(assets, run_id)
        result.stage3_count = len(assets)

        self._persist_active(assets)
        result.candidate_symbols = [a["symbol"] for a in assets]
        result.duration_seconds  = time.monotonic() - t_start

        log.info(
            "[universe] Full refresh complete: S1=%d S2=%d S3=%d  (%.1fs)",
            result.stage1_count, result.stage2_count, result.stage3_count,
            result.duration_seconds,
        )
        return result

    def run_rescore(self) -> UniverseRunResult:
        """Re-run Stage 3 only; Stage 2 survivors are loaded from DB."""
        run_id  = str(uuid.uuid4())
        t_start = time.monotonic()
        result  = UniverseRunResult(run_id=run_id, run_type="rescore")

        log.info("[universe] Starting Stage-3 re-score  run_id=%s", run_id)

        # Load existing Stage-2+ survivors + fixtures from DB
        df = get_universe_assets(active_only=True)
        if df.empty:
            log.warning("[universe] No active assets in DB — run full refresh first.")
            return result

        assets = df.to_dict("records")
        result.stage2_count = len(assets)

        assets = self._stage3_score(assets, run_id)
        result.stage3_count = len(assets)

        self._persist_active(assets)
        result.candidate_symbols = [a["symbol"] for a in assets]
        result.duration_seconds  = time.monotonic() - t_start

        log.info(
            "[universe] Re-score complete: %d candidates  (%.1fs)",
            result.stage3_count, result.duration_seconds,
        )
        return result

    def get_watchlist(self) -> list[str]:
        """
        Return the active candidate symbols from the DB.
        Falls back to config.data.watchlist when the universe table is empty.
        """
        df = get_universe_assets(active_only=True)
        if df.empty:
            log.warning(
                "[universe] No active assets found — using static watchlist. "
                "Run UniverseSelector.run_full() to populate."
            )
            return list(config.data.watchlist)
        return df["symbol"].tolist()

    # ── Stage 1 ───────────────────────────────────────────────────────────────

    def _stage1_fetch(self, run_id: str) -> list[dict]:
        """
        Pull all active, tradable US equities from Alpaca.
        Fixtures that fail the Alpaca lookup are still included.
        """
        t0      = time.monotonic()
        fixtures = set(self._cfg.permanent_fixtures)
        assets: list[dict] = []

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass as AlpacaAssetClass

            if not config.alpaca.api_key or not config.alpaca.secret_key:
                raise UniverseError(
                    "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — "
                    "cannot run Stage 1. Set env vars and retry."
                )

            client = TradingClient(
                api_key=config.alpaca.api_key,
                secret_key=config.alpaca.secret_key,
                paper=True,
            )
            raw = client.get_all_assets(
                GetAssetsRequest(asset_class=AlpacaAssetClass.US_EQUITY)
            )

            seen = 0
            for a in raw:
                if seen >= self._cfg.stage1_max:
                    break
                sym = getattr(a, "symbol", None)
                if sym is None:
                    continue
                # .status may be an AssetStatus enum — use .value to get "active"
                raw_status = getattr(a, "status", None)
                status = (
                    raw_status.value if hasattr(raw_status, "value")
                    else str(raw_status)
                ).lower()
                tradable = bool(getattr(a, "tradable", False))
                raw_exchange = getattr(a, "exchange", None)
                exchange = (
                    raw_exchange.value if hasattr(raw_exchange, "value")
                    else str(raw_exchange or "")
                ).upper()
                # Fixtures bypass all checks (ETFs on ARCA must be allowed through)
                if sym not in fixtures:
                    if status != "active" or not tradable:
                        continue
                    if self._cfg.allowed_exchanges and exchange not in self._cfg.allowed_exchanges:
                        continue
                assets.append({
                    "symbol":      sym,
                    "name":        getattr(a, "name", sym),
                    "asset_class": "etf" if sym in fixtures else "us_equity",
                    "is_fixture":  sym in fixtures,
                    "exchange":    exchange,
                })
                seen += 1

            log.info("[universe] Stage 1: %d assets from Alpaca", len(assets))

        except UniverseError:
            raise
        except Exception as exc:
            log.warning("[universe] Stage 1 Alpaca fetch failed (%s) — fixtures only", exc)
            assets = []

        # Always ensure fixtures are present
        existing_syms = {a["symbol"] for a in assets}
        for sym in fixtures:
            if sym not in existing_syms:
                assets.append({
                    "symbol":      sym,
                    "name":        sym,
                    "asset_class": "etf",
                    "is_fixture":  True,
                })

        duration = time.monotonic() - t0
        _log_stage(run_id, "full", 1, len(assets), duration, notes=None)
        return assets

    # ── Stage 2 ───────────────────────────────────────────────────────────────

    def _stage2_filter(self, assets: list[dict], run_id: str) -> list[dict]:
        """
        Two-pass filter — fixtures bypass both checks.

        Pass 1 (fast, batched): batch-download 20-day OHLCV via yf.download()
          in groups of 200.  Compute avg daily dollar volume = (close x volume).mean().
          Drop symbols below min_avg_dollar_volume.

        Pass 2 (targeted): fetch FundamentalsClient for the much smaller set that
          survived Pass 1.  Drop symbols below min_market_cap.
          Sort survivors by dollar volume desc, keep top stage2_max.
        """
        t0 = time.monotonic()
        fundamentals = FundamentalsClient()
        fixtures      = [a for a in assets if a.get("is_fixture")]
        non_fixtures  = [a for a in assets if not a.get("is_fixture")]

        # ── Pass 1: batch dollar-volume check ─────────────────────────────────
        dv_map: dict[str, float] = {}
        syms       = [a["symbol"] for a in non_fixtures]
        batch_size = 200

        n_batches = max(1, (len(syms) + batch_size - 1) // batch_size)
        log.info(
            "[universe] Stage 2: checking dollar volume for %d symbols "
            "in %d batches of %d ...",
            len(syms), n_batches, batch_size,
        )

        for batch_num, i in enumerate(range(0, len(syms), batch_size), start=1):
            batch = syms[i : i + batch_size]
            t_batch = time.monotonic()
            try:
                if len(batch) == 1:
                    raw = yf.Ticker(batch[0]).history(period="1mo")
                    if not raw.empty:
                        dv = (raw["Close"] * raw["Volume"]).tail(20).mean()
                        dv_map[batch[0]] = float(dv) if pd.notna(dv) else 0.0
                else:
                    raw = yf.download(
                        batch, period="1mo", group_by="ticker",
                        progress=False, auto_adjust=True, threads=True,
                    )
                    for sym in batch:
                        try:
                            close  = raw[sym]["Close"]
                            volume = raw[sym]["Volume"]
                            dv     = (close * volume).tail(20).mean()
                            dv_map[sym] = float(dv) if pd.notna(dv) else 0.0
                        except Exception:
                            dv_map[sym] = 0.0
            except Exception as exc:
                log.debug(
                    "[universe] Stage 2 batch DV download failed (offset=%d): %s", i, exc
                )
            elapsed = time.monotonic() - t_batch
            passing_so_far = sum(
                1 for s in syms[: i + len(batch)]
                if dv_map.get(s, 0.0) >= self._cfg.min_avg_dollar_volume
            )
            log.info(
                "[universe] Stage 2 batch %d/%d done (%.1fs)  "
                "symbols checked: %d  passing so far: %d",
                batch_num, n_batches, elapsed,
                min(i + batch_size, len(syms)),
                passing_so_far,
            )

        dv_passed = [
            a for a in non_fixtures
            if dv_map.get(a["symbol"], 0.0) >= self._cfg.min_avg_dollar_volume
        ]
        for a in dv_passed:
            a["avg_dollar_volume"] = dv_map[a["symbol"]]

        log.info(
            "[universe] Stage 2 pass 1: %d / %d passed dollar-volume filter ($%.0fM/day)",
            len(dv_passed), len(non_fixtures), self._cfg.min_avg_dollar_volume / 1e6,
        )

        # ── Pass 2: market-cap check (only dollar-volume survivors) ───────────
        cap_passed: list[dict] = []
        for asset in dv_passed:
            sym = asset["symbol"]
            try:
                fund    = fundamentals.get(sym)
                mkt_cap = fund.get("market_cap") if fund else None
                if mkt_cap is None or mkt_cap < self._cfg.min_market_cap:
                    continue
                asset["market_cap"] = mkt_cap
                cap_passed.append(asset)
            except Exception as exc:
                log.debug("[universe] Stage 2 market-cap failed for %s: %s", sym, exc)

        # Sort by dollar volume desc; keep top stage2_max non-fixtures
        cap_passed.sort(key=lambda a: a.get("avg_dollar_volume", 0.0), reverse=True)
        cap_passed = cap_passed[: self._cfg.stage2_max]

        # Fixtures: fill market cap best-effort (cached)
        for asset in fixtures:
            try:
                fund = fundamentals.get(asset["symbol"])
                asset["market_cap"] = fund.get("market_cap") if fund else None
            except Exception:
                asset["market_cap"] = None
            asset.setdefault("avg_dollar_volume", None)

        passed   = fixtures + cap_passed
        duration = time.monotonic() - t0
        log.info("[universe] Stage 2: %d total passed (%d fixtures + %d candidates)  (%.1fs)",
                 len(passed), len(fixtures), len(cap_passed), duration)
        _log_stage(run_id, "full", 2, len(passed), duration, notes=None)
        return passed

    # ── Stage 3 ───────────────────────────────────────────────────────────────

    def _stage3_score(self, assets: list[dict], run_id: str) -> list[dict]:
        """
        Score assets and keep the top stage3_max.

        If self._xgb is available, calls xgb_model.predict(indicator_df) per symbol.
        Otherwise sorts by market_cap descending (None treated as 0).
        Fixtures are always retained regardless of score / rank.
        """
        t0      = time.monotonic()
        run_type = "rescore" if run_id else "full"
        now     = datetime.now(timezone.utc).replace(tzinfo=None)

        fixtures      = [a for a in assets if a.get("is_fixture")]
        non_fixtures  = [a for a in assets if not a.get("is_fixture")]

        if self._xgb is not None:
            # Backfill OHLCV for any candidate lacking bars — otherwise Stage 3
            # would assign xgb_score=0.0 and systematically drop new entrants,
            # reinforcing whatever universe was tracked in previous runs.
            fetcher = DataFetcher()
            missing = [a["symbol"] for a in non_fixtures
                       if get_bars(a["symbol"], "1d", limit=1).empty]
            if missing:
                log.info("[universe] Stage 3: backfilling bars for %d new candidates", len(missing))
                t_bf = time.monotonic()
                backfilled = 0
                for sym in missing:
                    try:
                        df = fetcher.fetch_symbol(sym, interval="1d", days_back=365)
                        if not df.empty:
                            backfilled += 1
                    except Exception as exc:
                        log.debug("[universe] Stage 3 backfill failed for %s: %s", sym, exc)
                log.info("[universe] Stage 3: backfilled %d/%d candidates (%.1fs)",
                         backfilled, len(missing), time.monotonic() - t_bf)

            engine = IndicatorEngine()
            scored = 0
            zero_scored = 0
            for asset in non_fixtures:
                sym = asset["symbol"]
                try:
                    ind_df = engine.run(sym)
                    if ind_df.empty:
                        asset["xgb_score"] = 0.0
                        zero_scored += 1
                    else:
                        asset["xgb_score"] = float(self._xgb.predict(ind_df))
                        scored += 1
                except Exception as exc:
                    log.debug("[universe] Stage 3 XGB score failed for %s: %s", sym, exc)
                    asset["xgb_score"] = 0.0
                    zero_scored += 1
            log.info("[universe] Stage 3 scoring: %d scored, %d zero-score fallback",
                     scored, zero_scored)
            non_fixtures.sort(key=lambda a: a.get("xgb_score") or 0.0, reverse=True)
        else:
            for a in non_fixtures:
                a["xgb_score"] = None
            non_fixtures.sort(
                key=lambda a: a.get("market_cap") or 0.0, reverse=True
            )

        # Take top N non-fixtures; always keep all fixtures
        top_n    = non_fixtures[: self._cfg.stage3_max]
        selected = fixtures + top_n

        # Tag each asset with stage / timestamps
        for a in selected:
            a["stage"]         = 3
            a["active"]        = True
            a["last_scored_at"] = now
            a.setdefault("added_at", now)

        duration = time.monotonic() - t0
        log.info("[universe] Stage 3: %d candidates (including %d fixtures)",
                 len(selected), len(fixtures))
        _log_stage(run_id, run_type, 3, len(selected), duration, notes=None)
        return selected

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_active(self, assets: list[dict]) -> None:
        """
        Write new active list to DB.
        Symbols no longer in the list are marked inactive (removed_at set).
        """
        from data.database import get_engine, UniverseAsset
        from sqlalchemy.orm import Session

        new_syms = {a["symbol"] for a in assets}
        now      = datetime.now(timezone.utc).replace(tzinfo=None)

        # Upsert each selected asset
        for asset in assets:
            upsert_universe_asset(asset)

        # Mark removed assets inactive
        engine = get_engine()
        with Session(engine) as session:
            stale = (
                session.query(UniverseAsset)
                .filter(
                    UniverseAsset.active == True,   # noqa: E712
                    ~UniverseAsset.symbol.in_(new_syms),
                )
                .all()
            )
            stale_syms = [row.symbol for row in stale]   # read before session closes
            for row in stale:
                row.active     = False
                row.removed_at = now
            session.commit()

        if stale_syms:
            log.info("[universe] Marked %d assets inactive: %s",
                     len(stale_syms), stale_syms)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _log_stage(run_id: str, run_type: str, stage: int,
               count: int, duration: float, notes: str | None) -> None:
    """Write one row to universe_run_log."""
    log_universe_run({
        "run_id":           run_id,
        "run_type":         run_type,
        "stage":            stage,
        "symbol_count":     count,
        "duration_seconds": round(duration, 3),
        "recorded_at":      datetime.now(timezone.utc).replace(tzinfo=None),
        "notes":            notes,
    })
