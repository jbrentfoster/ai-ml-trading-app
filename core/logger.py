"""
Centralized logging for the trading app.
All modules should import get_logger() from here.

Two log namespaces write to logs/trading_app.log:
  - trading.*  — app code, INFO+ (or whatever LoggingConfig.level is set to)
  - root       — Streamlit, libraries, uncaught errors, WARNING+
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import config

_initialized = False


def _setup_root_logger() -> None:
    global _initialized
    if _initialized:
        return

    log_cfg = config.logging

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Shared rotating file handler ──────────────────────────────────────────
    file_handler: RotatingFileHandler | None = None
    if log_cfg.log_to_file:
        log_dir = Path(log_cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "trading_app.log",
            maxBytes=log_cfg.max_file_size_mb * 1024 * 1024,
            backupCount=log_cfg.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)

    # ── App logger: trading.* at configured level (default INFO) ──────────────
    app_logger = logging.getLogger("trading")
    app_logger.setLevel(getattr(logging, log_cfg.level.upper(), logging.INFO))
    app_logger.propagate = False   # prevent double-writing to root's handlers

    if log_cfg.log_to_console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        app_logger.addHandler(ch)

    if file_handler is not None:
        app_logger.addHandler(file_handler)

    # ── Root logger: WARNING+ from Streamlit, libraries, uncaught exceptions ──
    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    if file_handler is not None:
        # Re-use the same handler — RotatingFileHandler is thread-safe
        root.addHandler(file_handler)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger namespaced under 'trading'."""
    _setup_root_logger()
    return logging.getLogger(f"trading.{name}")
