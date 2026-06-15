"""IBKR Flex Query Web Service client — session-independent trade history.

WHY THIS EXISTS: the real-time API tier (``reqExecutions`` / ``reqCompletedOrders``)
returns only the *current* Gateway session's executions.  On this account the
Gateway resets overnight, so the morning Phase-B poll reliably ``fetched 0`` and
every between-run fill was lost from ``fill_log`` until manually Flex-recovered
(the 2026-06-08/09 escalation).  The Flex Web Service is a server-side statement
service that retains a year+ and is independent of the Gateway session, so a
daily fetch captures every prior-day fill regardless of overnight resets.

Empirically verified 2026-06-09 (see the chat that added this): a Month-to-Date
Trades-Execution query returns the same stable ``ibExecID`` that ``reqExecutions``
emits, so its rows dedup cleanly against ``fill_log``.  Two operational facts the
probe established and this client handles:
  - ``SendRequest`` is rate-throttled (error **1001**, "try again shortly") — retry
    with backoff.  ``GetStatement`` on an existing reference is NOT throttled.
  - Statement generation is async (error **1019**, "in progress") — poll until ready.
  - Flex is **T+1**: a statement reflects activity through the prior business day;
    *today's* fills appear in tomorrow's statement.  So the durable design is the
    daily Flex fetch as a backstop, with the in-session ``reqExecutions`` poll as a
    same-day latency optimisation.

This module only *fetches* the XML.  Parsing (``scripts/backfill_flex_trades.py:
parse_flex_trades``) and reconciliation (``execution/reconciliation.reconcile_fills``)
are reused unchanged by ``scripts/reconcile_flex.py``.

The two HTTP calls and the sleep are injectable so the retry/poll state machine is
unit-testable without network.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from typing import Callable

from core.logger import get_logger

log = get_logger("data.flex_client")

_FLEX_BASE = (
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
)

# IBKR Flex error codes we treat specially (everything else → hard fail).
_THROTTLE_CODE = "1001"      # SendRequest: "Statement could not be generated … try again shortly"
_IN_PROGRESS_CODE = "1019"   # GetStatement: "Statement generation in progress"


class FlexError(RuntimeError):
    """Raised when the Flex Web Service cannot produce a statement."""


def _default_http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "trading_app-flex/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        # DNS / connection / timeout failures (e.g. socket.gaierror "[Errno 11001]
        # getaddrinfo failed") raise raw URLError/OSError.  Re-raise as FlexError so
        # all transport failures funnel through the single failure type callers
        # already handle (reconcile_flex.py logs + exits 0 on FlexError).
        raise FlexError(f"Flex HTTP request failed: {exc}") from exc


def _tag(xml: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<]*)</{name}>", xml)
    return m.group(1).strip() if m else None


def _looks_like_statement(body: str) -> bool:
    """A delivered statement is a FlexQueryResponse (has <Trade …> or is empty-but-typed).

    A *not-ready* / error reply is a small FlexStatementResponse carrying an
    <ErrorCode>.  We treat presence of FlexQueryResponse (or any <Trade ) as
    "delivered", so an empty-but-valid statement (no trades in the window) is
    correctly distinguished from an in-progress/error reply.
    """
    return "<FlexQueryResponse" in body or "<Trade " in body


def fetch_flex_statement(
    token: str,
    query_id: str,
    *,
    http_get: Callable[[str], str] = _default_http_get,
    sleep: Callable[[float], None] = time.sleep,
    send_attempts: int = 6,
    send_backoff_s: float = 20.0,
    get_attempts: int = 8,
    get_backoff_s: float = 4.0,
) -> str:
    """Run the 2-step Flex flow and return the statement XML text.

    SendRequest (retry on 1001 throttle) → GetStatement (poll on 1019 in-progress).
    Raises ``FlexError`` on missing credentials, a non-retryable error code, or
    exhausted retries.  ``http_get``/``sleep`` are injectable for tests.
    """
    if not token or not query_id:
        raise FlexError("Flex token and query_id are both required")

    # ── Step 1: SendRequest → ReferenceCode (+ delivery Url) ──────────────────
    ref = None
    base_url = None
    for attempt in range(1, send_attempts + 1):
        body = http_get(f"{_FLEX_BASE}.SendRequest?t={token}&q={query_id}&v=3")
        ref = _tag(body, "ReferenceCode")
        if ref:
            base_url = _tag(body, "Url") or f"{_FLEX_BASE}.GetStatement"
            break
        code = _tag(body, "ErrorCode")
        msg = _tag(body, "ErrorMessage") or body[:200]
        if code == _THROTTLE_CODE:
            log.warning(
                "Flex SendRequest throttled (1001), attempt %d/%d — retrying in %.0fs",
                attempt, send_attempts, send_backoff_s,
            )
            if attempt < send_attempts:
                sleep(send_backoff_s)
            continue
        raise FlexError(f"Flex SendRequest failed (code={code}): {msg}")

    if not ref:
        raise FlexError(
            f"Flex SendRequest throttled (1001) after {send_attempts} attempts"
        )

    # ── Step 2: GetStatement → poll until generated ───────────────────────────
    get_url = f"{base_url}?t={token}&q={ref}&v=3"
    for attempt in range(1, get_attempts + 1):
        body = http_get(get_url)
        if _looks_like_statement(body):
            log.info("Flex statement retrieved (ref=%s, %d bytes)", ref, len(body))
            return body
        code = _tag(body, "ErrorCode")
        if code == _IN_PROGRESS_CODE or "in progress" in body.lower():
            log.info(
                "Flex statement still generating (1019), attempt %d/%d", attempt, get_attempts
            )
            if attempt < get_attempts:
                sleep(get_backoff_s)
            continue
        msg = _tag(body, "ErrorMessage") or body[:200]
        raise FlexError(f"Flex GetStatement failed (code={code}): {msg}")

    raise FlexError(
        f"Flex statement still generating after {get_attempts} GetStatement attempts (ref={ref})"
    )
