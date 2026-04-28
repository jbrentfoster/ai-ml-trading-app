"""
Position sizer — fractional Kelly criterion with ATR-based stops.

Kelly criterion:
    f* = (p * b - q) / b
    where p = win rate, b = avg_win / avg_loss, q = 1 - p

Fractional Kelly:
    position_pct = kelly_fraction * f*
    (capped at kelly_max_position_pct; floored at 0)

Stop / take-profit placement:
    BUY:  stop  = entry - atr * atr_stop_multiplier
          tp    = entry + atr * atr_take_profit_multiplier
    SELL: stop  = entry + atr * atr_stop_multiplier
          tp    = entry - atr * atr_take_profit_multiplier
    Fallback when ATR = 0: fixed stop at fixed_stop_loss_pct distance.

If there is insufficient signal-log history (< kelly_min_trades), the sizer
falls back to a fixed-stop sizing: risk 1% of equity per trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from config.settings import config
from core.logger import get_logger

log = get_logger("risk.position_sizer")


@dataclass
class PositionSize:
    symbol:            str
    signal:            str         # "BUY" | "SELL"
    shares:            int
    entry_price:       float
    stop_price:        float
    take_profit_price: float
    position_value:    float
    position_pct:      float       # fraction of equity
    kelly_fraction_used: float     # effective kelly fraction (0 if fallback)
    method:            str         # "kelly" | "fixed"


class PositionSizer:

    def __init__(self) -> None:
        self._cfg     = config.risk
        self._trading = config.trading

    def calculate(
        self,
        symbol: str,
        signal: str,
        equity: float,
        entry_price: float,
        atr: float | None = None,
    ) -> PositionSize:
        """
        Calculate a position size for `signal` ("BUY" or "SELL") given
        current `equity` and `entry_price`.  `atr` is the 14-bar ATR;
        pass None or 0 to use the fixed-stop fallback.
        """
        atr_val = atr if atr and atr > 0 else 0.0

        # ── Stop / take-profit ────────────────────────────────────────────────
        stop_price, tp_price = self._compute_stops(
            signal, entry_price, atr_val
        )

        # ── Position size via Kelly ───────────────────────────────────────────
        # Investable equity = total equity minus the cash reserve.
        # Kelly fractions are applied against this reduced base so positions
        # are naturally smaller when a cash reserve is configured.
        investable_equity = equity * (1.0 - max(self._trading.cash_reserve_pct, 0.0))

        kelly_f, method = self._kelly_fraction(symbol)
        # Cap at both the Kelly max and the portfolio guard's hard size limit
        # so the sizer never proposes a position the guard will always reject.
        hard_cap = min(self._cfg.kelly_max_position_pct,
                       self._trading.max_position_size_pct)
        position_pct = min(kelly_f, hard_cap)
        position_pct = max(position_pct, 0.0)

        position_value = position_pct * investable_equity
        shares = max(int(position_value / entry_price), 0) if entry_price > 0 else 0

        log.debug(
            "[%s] %s size: shares=%d value=%.0f (%.1f%% equity) method=%s",
            symbol, signal, shares, position_value, position_pct * 100, method,
        )

        return PositionSize(
            symbol=symbol,
            signal=signal,
            shares=shares,
            entry_price=entry_price,
            stop_price=stop_price,
            take_profit_price=tp_price,
            position_value=position_value,
            position_pct=position_pct,
            kelly_fraction_used=kelly_f,
            method=method,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _compute_stops(
        self, signal: str, entry: float, atr: float
    ) -> tuple[float, float]:
        """Return (stop_price, take_profit_price)."""
        if atr > 0:
            stop_dist = atr * self._cfg.atr_stop_multiplier
            tp_dist   = atr * self._cfg.atr_take_profit_multiplier
        else:
            stop_dist = entry * self._cfg.fixed_stop_loss_pct
            tp_dist   = entry * self._cfg.fixed_stop_loss_pct * 2.0

        if signal == "BUY":
            return entry - stop_dist, entry + tp_dist
        else:  # SELL
            return entry + stop_dist, entry - tp_dist

    def _kelly_fraction(self, symbol: str) -> tuple[float, str]:
        """
        Return (effective_kelly_fraction, method_label).

        Reads from the signal_log to compute win rate and win/loss ratio.
        Falls back to a fixed 1%-of-equity risk sizing when insufficient history.
        """
        try:
            from data.database import get_engine, SignalLog
            from sqlalchemy.orm import Session as _Session

            engine = get_engine()
            min_trades = self._cfg.kelly_min_trades

            with _Session(engine) as session:
                rows = (
                    session.query(SignalLog)
                    .filter(
                        SignalLog.symbol == symbol,
                        SignalLog.passed_gate == True,  # noqa: E712
                    )
                    .order_by(SignalLog.bar_timestamp.desc())
                    .limit(200)
                    .all()
                )
                # Read all needed attributes inside session
                scores = [
                    (r.ensemble_score, r.signal) for r in rows
                ]

            if len(scores) < min_trades:
                return self._fixed_kelly_equivalent(), "fixed"

            wins  = [abs(s) for s, sig in scores if s > 0]
            losses = [abs(s) for s, sig in scores if s < 0]

            if not wins or not losses:
                return self._fixed_kelly_equivalent(), "fixed"

            p = len(wins) / len(scores)
            q = 1 - p
            b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))

            if b <= 0:
                return self._fixed_kelly_equivalent(), "fixed"

            f_star = (p * b - q) / b
            f_frac = max(f_star * self._cfg.kelly_fraction, 0.0)
            return f_frac, "kelly"

        except Exception as exc:
            log.warning("Kelly calculation failed for %s: %s — using fixed sizing", symbol, exc)
            return self._fixed_kelly_equivalent(), "fixed"

    def _fixed_kelly_equivalent(self) -> float:
        """
        Fixed-stop equivalent position size.
        Risk 1% of equity per trade at `fixed_stop_loss_pct` stop distance.
        position_pct = 0.01 / fixed_stop_loss_pct
        """
        return 0.01 / max(self._cfg.fixed_stop_loss_pct, 0.001)
