"""
Risk & portfolio management package (Phase 4).

Modules
-------
position_sizer  — fractional Kelly criterion position sizing with ATR stops
portfolio_guard — 6-check sequential pre-trade guard
circuit_breaker — SQLite-persisted trading halt with auto-reset
order_manager   — orchestrates sizer → guard → order submission lifecycle
trailing_stop   — converts qualifying bracket TPs into standalone TRAIL orders
"""

from risk.circuit_breaker import CircuitBreaker
from risk.order_manager import OrderDecision, OrderManager
from risk.portfolio_guard import GuardResult, PortfolioGuard
from risk.position_sizer import PositionSize, PositionSizer
from risk.trailing_stop import TrailingStopAction, TrailingStopManager

__all__ = [
    "CircuitBreaker",
    "OrderDecision",
    "OrderManager",
    "GuardResult",
    "PortfolioGuard",
    "PositionSize",
    "PositionSizer",
    "TrailingStopAction",
    "TrailingStopManager",
]
