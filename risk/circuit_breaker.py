"""
Circuit breaker — SQLite-persisted trading halt.

Triggers:
  - Manual: CircuitBreaker.trigger(reason)
  - Automatic: when daily_loss_pct >= config.risk.circuit_breaker_daily_loss_pct
               or weekly_loss_pct >= config.risk.circuit_breaker_weekly_loss_pct

Auto-reset:
  On each call to is_halted(), if the most recent TRIGGERED event is older
  than circuit_breaker_reset_hours, the breaker is automatically reset and
  an AUTO_RESET event is logged.

Manual reset:
  CircuitBreaker.reset() logs a RESET event unconditionally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config.settings import config
from core.logger import get_logger
from data.database import (
    get_circuit_breaker_log,
    get_latest_circuit_breaker_event,
    log_circuit_breaker_event,
)

log = get_logger("risk.circuit_breaker")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CircuitBreaker:

    def __init__(self) -> None:
        self._cfg = config.risk

    def is_halted(self) -> tuple[bool, str]:
        """
        Return (halted: bool, reason: str).

        Checks the most recent log entry:
        - TRIGGERED and within reset window → halted
        - TRIGGERED but past reset window   → auto-reset, not halted
        - RESET / AUTO_RESET (or no entry)  → not halted
        """
        event = get_latest_circuit_breaker_event()

        if event is None or event["event"] in ("RESET", "AUTO_RESET"):
            return False, ""

        # Most recent event is TRIGGERED
        triggered_at = event["triggered_at"] or event["recorded_at"]
        age_hours = ((_now() - triggered_at).total_seconds()) / 3600

        if age_hours >= self._cfg.circuit_breaker_reset_hours:
            # Auto-reset
            now = _now()
            log_circuit_breaker_event({
                "event":           "AUTO_RESET",
                "reason":          f"Auto-reset after {age_hours:.1f}h (limit: {self._cfg.circuit_breaker_reset_hours}h)",
                "daily_loss_pct":  None,
                "weekly_loss_pct": None,
                "triggered_at":    triggered_at,
                "reset_at":        now,
                "recorded_at":     now,
            })
            log.info("Circuit breaker auto-reset after %.1fh", age_hours)
            return False, ""

        reason = event.get("reason") or "Circuit breaker triggered"
        return True, reason

    def trigger(
        self,
        reason: str,
        daily_loss_pct: float = 0.0,
        weekly_loss_pct: float = 0.0,
    ) -> None:
        """Trigger a trading halt and log the event."""
        now = _now()
        log_circuit_breaker_event({
            "event":           "TRIGGERED",
            "reason":          reason,
            "daily_loss_pct":  daily_loss_pct,
            "weekly_loss_pct": weekly_loss_pct,
            "triggered_at":    now,
            "reset_at":        None,
            "recorded_at":     now,
        })
        log.warning("Circuit breaker TRIGGERED: %s", reason)

    def reset(self) -> None:
        """Manually clear the halt state."""
        now = _now()
        # Carry forward triggered_at from the most recent trigger
        last = get_latest_circuit_breaker_event()
        triggered_at = None
        if last and last["event"] == "TRIGGERED":
            triggered_at = last["triggered_at"]

        log_circuit_breaker_event({
            "event":           "RESET",
            "reason":          "Manual reset",
            "daily_loss_pct":  None,
            "weekly_loss_pct": None,
            "triggered_at":    triggered_at,
            "reset_at":        now,
            "recorded_at":     now,
        })
        log.info("Circuit breaker manually RESET")

    def check_loss_limits(
        self, daily_loss_pct: float, weekly_loss_pct: float
    ) -> bool:
        """
        Trigger the breaker if loss thresholds are breached.
        Returns True if the breaker was triggered (new or existing).
        """
        halted, _ = self.is_halted()
        if halted:
            return True

        triggered = False
        reasons = []

        if abs(daily_loss_pct) >= self._cfg.circuit_breaker_daily_loss_pct:
            reasons.append(
                f"Daily loss {daily_loss_pct:.1%} >= limit {self._cfg.circuit_breaker_daily_loss_pct:.1%}"
            )
            triggered = True

        if abs(weekly_loss_pct) >= self._cfg.circuit_breaker_weekly_loss_pct:
            reasons.append(
                f"Weekly loss {weekly_loss_pct:.1%} >= limit {self._cfg.circuit_breaker_weekly_loss_pct:.1%}"
            )
            triggered = True

        if triggered:
            self.trigger(
                reason="; ".join(reasons),
                daily_loss_pct=daily_loss_pct,
                weekly_loss_pct=weekly_loss_pct,
            )

        return triggered

    def get_status(self) -> dict:
        """Return a status dict suitable for dashboard display."""
        halted, reason = self.is_halted()
        event = get_latest_circuit_breaker_event()
        return {
            "halted":          halted,
            "reason":          reason,
            "last_event":      event.get("event") if event else None,
            "triggered_at":    event.get("triggered_at") if event else None,
            "reset_at":        event.get("reset_at") if event else None,
            "recorded_at":     event.get("recorded_at") if event else None,
            "daily_loss_pct":  event.get("daily_loss_pct") if event else None,
            "weekly_loss_pct": event.get("weekly_loss_pct") if event else None,
        }
