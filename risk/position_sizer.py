"""
Position sizer — fractional Kelly criterion with ATR-based stops.

Kelly criterion:
    f* = (p * b - q) / b
    where p = win rate, b = avg_win / avg_loss, q = 1 - p

Fractional Kelly:
    position_pct = kelly_fraction * f*
    (capped at kelly_max_position_pct; floored at 0)

Two Kelly sources are supported:
    1. ``kelly_realised`` — derived from the realised pnl_pct of closed trades
       in ``trade_log`` via :func:`compute_realised_kelly`.  This is the
       Phase C path; it kicks in once a symbol has at least
       ``RiskConfig.min_trades_for_realised_kelly`` closed trades.
    2. ``kelly_proxy`` — the legacy cold-start fallback.  Reads ensemble
       scores from ``signal_log`` and treats ``ensemble_score > 0`` as a
       win, ``< 0`` as a loss.  Used until enough realised history has
       accumulated.

Stop / take-profit placement:
    BUY:  stop  = entry - atr * atr_stop_multiplier
          tp    = entry + atr * atr_take_profit_multiplier
    SELL: stop  = entry + atr * atr_stop_multiplier
          tp    = entry - atr * atr_take_profit_multiplier
    Fallback when ATR = 0: fixed stop at fixed_stop_loss_pct distance.

If neither realised nor proxy Kelly is available, the sizer falls back to a
fixed-stop sizing: risk 1% of equity per trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
    method:            str         # "kelly_realised" | "kelly_proxy" | "fixed"


def compute_realised_kelly(
    symbol: str,
    as_of: datetime | None = None,
    lookback_n: int = 100,
    source: str | None = None,
    run_id: str | None = None,
) -> dict | None:
    """
    Compute realised-Kelly statistics from closed trades in ``trade_log``.

    Returns a dict with keys ``n_trades``, ``win_rate``, ``avg_win_pct``,
    ``avg_loss_pct``, ``b``, ``f_star`` — or ``None`` when no trades match
    the filter (treated as cold start by callers).

    All metrics are derived from ``pnl_pct`` (signed fractional return on the
    position).  Winners are ``pnl_pct > 0``, losers are ``pnl_pct < 0``;
    zero-P&L trades are excluded from the win/loss aggregates but still count
    towards ``n_trades``.

    Forward-only safety: when ``as_of`` is provided, only trades with
    ``entry_ts < as_of`` are considered.  This is what the walk-forward
    orchestrator relies on to ensure a fold cannot see its own trades.

    When the matched window contains all wins or all losses, Kelly is
    undefined: ``b`` and ``f_star`` are returned as ``None`` and the caller
    falls back to the cold-start path.
    """
    try:
        from data.database import get_engine, TradeLog
        from sqlalchemy import desc as _desc
        from sqlalchemy.orm import Session as _Session
    except Exception:
        return None

    try:
        engine = get_engine()
        with _Session(engine) as session:
            q = session.query(TradeLog).filter(TradeLog.symbol == symbol)
            if as_of is not None:
                q = q.filter(TradeLog.entry_ts < as_of)
            if source is not None:
                q = q.filter(TradeLog.source == source)
            if run_id is not None:
                q = q.filter(TradeLog.run_id == run_id)
            q = q.order_by(_desc(TradeLog.entry_ts)).limit(lookback_n)
            rows = q.all()
            pnl_pcts = [float(r.pnl_pct) for r in rows]
    except Exception as exc:
        log.warning("compute_realised_kelly DB error for %s: %s", symbol, exc)
        return None

    if not pnl_pcts:
        return None

    wins   = [x for x in pnl_pcts if x > 0]
    losses = [-x for x in pnl_pcts if x < 0]   # absolute values
    n      = len(pnl_pcts)
    n_w    = len(wins)
    n_l    = len(losses)

    avg_win  = (sum(wins)   / n_w) if n_w else 0.0
    avg_loss = (sum(losses) / n_l) if n_l else 0.0

    if n_w == 0 or n_l == 0 or avg_loss <= 0:
        # Kelly is undefined — return the inputs so callers can see the data
        # exists, but mark b/f_star as None so they fall back.
        return {
            "n_trades":     n,
            "win_rate":     n_w / n if n > 0 else 0.0,
            "avg_win_pct":  avg_win,
            "avg_loss_pct": avg_loss,
            "b":            None,
            "f_star":       None,
        }

    b      = avg_win / avg_loss
    p      = n_w / n
    f_star = (p * b - (1 - p)) / b   # may be negative when edge is unfavourable

    return {
        "n_trades":     n,
        "win_rate":     p,
        "avg_win_pct":  avg_win,
        "avg_loss_pct": avg_loss,
        "b":            b,
        "f_star":       f_star,
    }


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
        kelly_history: dict | None = None,
    ) -> PositionSize:
        """
        Calculate a position size for ``signal`` ("BUY" or "SELL") given
        current ``equity`` and ``entry_price``.  ``atr`` is the 14-bar ATR;
        pass ``None`` or 0 to use the fixed-stop fallback.

        ``kelly_history`` is the optional output of
        :func:`compute_realised_kelly` — when provided AND the symbol has
        ``>= RiskConfig.min_trades_for_realised_kelly`` closed trades AND
        Kelly is well-defined, the sizer uses realised win-rate / avg-win /
        avg-loss to compute ``f*``.  Below that threshold, or when
        ``kelly_history is None``, the sizer falls back to the legacy
        signal_log proxy and finally to a fixed-stop sizing.
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

        kelly_f, method = self._kelly_fraction(symbol, kelly_history)
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

    def _kelly_fraction(
        self,
        symbol: str,
        kelly_history: dict | None = None,
    ) -> tuple[float, str]:
        """Return ``(effective_kelly_fraction, method_label)``.

        Priority:
          1. ``kelly_realised`` — realised history with ``n_trades >=
             min_trades_for_realised_kelly`` and a defined ``f_star``.
          2. ``kelly_proxy``    — signal_log ``|ensemble_score|`` proxy.
          3. ``fixed``          — fallback when neither produces a usable
             estimate.
        """
        if kelly_history is not None:
            n_trades = int(kelly_history.get("n_trades", 0) or 0)
            f_star   = kelly_history.get("f_star")
            if (
                n_trades >= self._cfg.min_trades_for_realised_kelly
                and f_star is not None
            ):
                f_frac = max(float(f_star) * self._cfg.kelly_fraction, 0.0)
                return f_frac, "kelly_realised"

        return self._kelly_fraction_proxy(symbol)

    def _kelly_fraction_proxy(self, symbol: str) -> tuple[float, str]:
        """
        Cold-start ``|ensemble_score|`` proxy from ``signal_log``.

        This is the legacy path used before realised P&L was plumbed through
        ``trade_log``.  It treats positive ensemble scores as wins and
        negative as losses; the resulting Kelly is a quality-of-signal proxy,
        not a P&L-based estimate.
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
            return f_frac, "kelly_proxy"

        except Exception as exc:
            log.warning("Kelly proxy calculation failed for %s: %s — using fixed sizing", symbol, exc)
            return self._fixed_kelly_equivalent(), "fixed"

    def _fixed_kelly_equivalent(self) -> float:
        """
        Fixed-stop equivalent position size.
        Risk 1% of equity per trade at `fixed_stop_loss_pct` stop distance.
        position_pct = 0.01 / fixed_stop_loss_pct
        """
        return 0.01 / max(self._cfg.fixed_stop_loss_pct, 0.001)
