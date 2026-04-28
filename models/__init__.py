"""
models package — ML signal generation layer (Step 3).

Public API:
    SignalGate      — three-filter signal gate
    SignalResult    — dataclass returned by SignalGate.evaluate()
    RegimeType      — TRENDING | MEAN_REVERTING | HIGH_VOLATILITY
"""

from models.signal_gate import SignalGate, SignalResult
from models.regime_detector import RegimeType

__all__ = ["SignalGate", "SignalResult", "RegimeType"]
