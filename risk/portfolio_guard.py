"""
Portfolio guard — 7-check sequential pre-trade gate.

Checks (run in order; first failure stops evaluation):
  1. circuit_breaker   — trading halt active?
  2. stop_sanity       — stop price on the loss side of entry for this signal?
  3. portfolio_drawdown — daily P&L loss >= max_portfolio_drawdown_pct?
  4. position_size     — proposed position > max_position_size_pct of equity?
  5. sector_exposure   — sector total would exceed max_sector_exposure_pct?
  6. correlation       — too many highly correlated positions already held?
  7. no_duplicate      — symbol (or GOOG/GOOGL pair) already in positions?

GOOG / GOOGL special case:
  GOOG and GOOGL are treated as the same underlying company.
  Holding either one blocks the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config.settings import config
from core.logger import get_logger
from risk.circuit_breaker import CircuitBreaker
from risk.position_sizer import PositionSize

log = get_logger("risk.portfolio_guard")

# Sector mapping for common tickers; "Unknown" means no sector check is applied.
_SECTOR_MAP: dict[str, str] = {
    # ETF fixtures
    "SPY": "Broad Market", "QQQ": "Broad Market",
    "IWM": "Broad Market", "DIA": "Broad Market",
    "XLF": "Financials",   "XLE": "Energy",
    "XLK": "Technology",   "XLV": "Healthcare",
    "XLI": "Industrials",  "XLP": "Consumer Staples",
    "XLY": "Consumer Disc", "XLU": "Utilities",
    "XLB": "Materials",    "XLRE": "Real Estate",
    "TLT": "Fixed Income", "GLD": "Commodities",
    "SLV": "Commodities",  "USO": "Commodities",
    # Large caps
    "AAPL": "Technology",  "MSFT": "Technology",
    "GOOGL": "Technology", "GOOG": "Technology",
    "META": "Technology",  "NVDA": "Technology",
    "AMZN": "Consumer Disc", "TSLA": "Consumer Disc",
    "JPM": "Financials",   "BAC": "Financials",
    "GS": "Financials",    "V": "Financials",
    "MA": "Financials",    "BRK.B": "Financials",
    "UNH": "Healthcare",   "JNJ": "Healthcare",
    "LLY": "Healthcare",   "ABBV": "Healthcare",
    "XOM": "Energy",       "CVX": "Energy",
    "WMT": "Consumer Staples", "PG": "Consumer Staples",
    "KO": "Consumer Staples",  "PEP": "Consumer Staples",
    "T": "Telecom",        "VZ": "Telecom",
}

# GOOG / GOOGL are the same underlying
_GOOG_PAIR = {"GOOG", "GOOGL"}


@dataclass
class GuardResult:
    passed: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)  # check name → passed


class PortfolioGuard:

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        self._cfg = config.risk
        self._trading = config.trading
        self._cb  = circuit_breaker or CircuitBreaker()

    def check(
        self,
        symbol: str,
        signal: str,
        position_size: PositionSize,
        equity: float,
        positions: dict,          # symbol → {shares, entry_price, current_price, ...}
        daily_pnl_pct: float = 0.0,
    ) -> GuardResult:
        """
        Run all 6 sequential checks.  Returns on first failure.

        `positions` maps symbol → dict with at least `current_price` and `shares`.
        `daily_pnl_pct` is today's portfolio return (negative = loss), e.g. -0.025.
        """
        checks: dict[str, bool] = {}

        # ── 1. Circuit breaker ────────────────────────────────────────────────
        halted, cb_reason = self._cb.is_halted()
        checks["circuit_breaker"] = not halted
        if halted:
            return GuardResult(
                passed=False,
                reason=f"Circuit breaker active: {cb_reason}",
                checks=checks,
            )

        # ── 2. Stop-price sanity ──────────────────────────────────────────────
        stop_ok, stop_reason = self._check_stop_sanity(signal, position_size)
        checks["stop_sanity"] = stop_ok
        if not stop_ok:
            return GuardResult(passed=False, reason=stop_reason, checks=checks)

        # ── 3. Portfolio drawdown ─────────────────────────────────────────────
        max_dd = self._trading.max_portfolio_drawdown_pct
        drawdown_ok = daily_pnl_pct >= -max_dd
        checks["portfolio_drawdown"] = drawdown_ok
        if not drawdown_ok:
            return GuardResult(
                passed=False,
                reason=(
                    f"Daily portfolio loss {daily_pnl_pct:.1%} exceeds "
                    f"limit -{max_dd:.1%}"
                ),
                checks=checks,
            )

        # ── 4. Position size ──────────────────────────────────────────────────
        max_pos = self._trading.max_position_size_pct
        pos_pct = position_size.position_value / equity if equity > 0 else 0.0
        size_ok = pos_pct <= max_pos
        checks["position_size"] = size_ok
        if not size_ok:
            return GuardResult(
                passed=False,
                reason=(
                    f"Position {pos_pct:.1%} of equity exceeds limit {max_pos:.1%}"
                ),
                checks=checks,
            )

        # ── 5. Sector exposure ────────────────────────────────────────────────
        sector_ok, sector_reason = self._check_sector(
            symbol, position_size.position_value, equity, positions
        )
        checks["sector_exposure"] = sector_ok
        if not sector_ok:
            return GuardResult(passed=False, reason=sector_reason, checks=checks)

        # ── 6. Correlation ────────────────────────────────────────────────────
        corr_ok, corr_reason = self._check_correlation(symbol, positions)
        checks["correlation"] = corr_ok
        if not corr_ok:
            return GuardResult(passed=False, reason=corr_reason, checks=checks)

        # ── 7. No duplicate position ──────────────────────────────────────────
        dup_ok, dup_reason = self._check_duplicate(symbol, positions)
        checks["no_duplicate"] = dup_ok
        if not dup_ok:
            return GuardResult(passed=False, reason=dup_reason, checks=checks)

        return GuardResult(passed=True, reason="All checks passed", checks=checks)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_stop_sanity(
        self, signal: str, position_size: PositionSize
    ) -> tuple[bool, str]:
        """
        Verify the stop price sits on the loss side of entry for this signal.

        A bad ATR (NaN → 0, or a sign bug in stop placement) can yield a stop
        at or on the wrong side of the entry price, which would convert the
        intended safety stop into an instant or inverse-direction trigger.
        """
        entry = position_size.entry_price
        stop  = position_size.stop_price

        if entry <= 0:
            return False, f"Invalid entry price {entry:.4f}"
        if stop <= 0:
            return False, f"Invalid stop price {stop:.4f}"

        if signal == "BUY" and stop >= entry:
            return False, (
                f"BUY stop {stop:.4f} must be below entry {entry:.4f} "
                f"(stop on wrong side — check ATR / stop placement)"
            )
        if signal == "SELL" and stop <= entry:
            return False, (
                f"SELL stop {stop:.4f} must be above entry {entry:.4f} "
                f"(stop on wrong side — check ATR / stop placement)"
            )
        return True, ""

    def _check_sector(
        self,
        symbol: str,
        new_value: float,
        equity: float,
        positions: dict,
    ) -> tuple[bool, str]:
        """Return (ok, reason).  Passes when sector is unknown."""
        sector = _SECTOR_MAP.get(symbol.upper())
        if not sector:
            return True, ""  # unknown sector → no block

        max_pct = self._cfg.max_sector_exposure_pct
        existing = sum(
            pos.get("current_price", pos.get("entry_price", 0)) * pos.get("shares", 0)
            for sym, pos in positions.items()
            if _SECTOR_MAP.get(sym.upper()) == sector
        )
        total = (existing + new_value) / equity if equity > 0 else 0.0
        if total > max_pct:
            return (
                False,
                f"Sector '{sector}' exposure {total:.1%} would exceed limit {max_pct:.1%}",
            )
        return True, ""

    def _check_correlation(
        self, symbol: str, positions: dict
    ) -> tuple[bool, str]:
        """
        Return (ok, reason).

        Computes pairwise Pearson correlation of daily returns between `symbol`
        and each held position over the last `correlation_lookback_bars` bars.
        Passes if < max_correlated_positions have correlation > threshold.
        """
        if not positions:
            return True, ""

        max_corr = self._cfg.max_correlated_positions
        threshold = self._cfg.correlation_threshold
        lookback  = self._cfg.correlation_lookback_bars

        try:
            from data.database import get_bars

            sym_bars = get_bars(symbol, "1d", limit=lookback + 1)
            if sym_bars.empty or len(sym_bars) < 2:
                return True, ""  # no data → no block

            sym_returns = sym_bars["Close"].pct_change().dropna()
            highly_correlated: list[str] = []

            for held_sym in list(positions.keys()):
                if held_sym == symbol:
                    continue
                held_bars = get_bars(held_sym, "1d", limit=lookback + 1)
                if held_bars.empty or len(held_bars) < 2:
                    continue
                held_returns = held_bars["Close"].pct_change().dropna()

                # Align on common dates
                aligned = pd.concat(
                    [sym_returns.rename("s"), held_returns.rename("h")], axis=1
                ).dropna()
                if len(aligned) < 10:
                    continue

                corr = aligned["s"].corr(aligned["h"])
                if corr >= threshold:
                    highly_correlated.append(held_sym)

            if len(highly_correlated) >= max_corr:
                return (
                    False,
                    f"{symbol} highly correlated (r>={threshold}) with "
                    f"{highly_correlated[:3]} — {len(highly_correlated)} positions at limit {max_corr}",
                )
        except Exception as exc:
            log.warning("Correlation check skipped for %s: %s", symbol, exc)

        return True, ""

    def _check_duplicate(
        self, symbol: str, positions: dict
    ) -> tuple[bool, str]:
        """
        Block if symbol already held, or if one of GOOG/GOOGL is held and
        the other is being requested.
        """
        if symbol in positions:
            return False, f"Already holding {symbol}"

        if symbol.upper() in _GOOG_PAIR:
            for held in positions:
                if held.upper() in _GOOG_PAIR and held.upper() != symbol.upper():
                    return False, f"Holding {held} (same underlying as {symbol})"

        return True, ""
