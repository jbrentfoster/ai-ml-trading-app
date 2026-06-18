"""Unit tests for data/flex_client.py — the Flex Web Service retry/poll state machine.

Network + sleep are injected, so these run offline and instantly.  The fake
``http_get`` routes on endpoint (SendRequest vs GetStatement) and pops a queued
response per call, letting each test script the exact 1001/1019 sequence.
"""

import urllib.error

import pytest

from data.flex_client import FlexError, _default_http_get, fetch_flex_statement

# ── canned Flex responses ──────────────────────────────────────────────────────
SEND_THROTTLE = (
    "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1001</ErrorCode>"
    "<ErrorMessage>Statement could not be generated at this time. Please try again shortly.</ErrorMessage>"
    "</FlexStatementResponse>"
)
SEND_OK = (
    "<FlexStatementResponse><Status>Success</Status><ReferenceCode>9876543210</ReferenceCode>"
    "<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>"
    "</FlexStatementResponse>"
)
SEND_FATAL = (
    "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1003</ErrorCode>"
    "<ErrorMessage>Statement is not available.</ErrorMessage></FlexStatementResponse>"
)
GET_INPROGRESS = (
    "<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
    "<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>"
    "</FlexStatementResponse>"
)
GET_OK = (
    '<FlexQueryResponse queryName="Trade History" type="AF"><FlexStatements count="1">'
    '<Trade ibExecID="0000e0d5.6a2187ad.01.01" symbol="IWM" buySell="BUY" quantity="80" '
    'tradePrice="290.0" dateTime="20260608;101543" levelOfDetail="EXECUTION"/>'
    "</FlexStatements></FlexQueryResponse>"
)
GET_OK_EMPTY = (
    '<FlexQueryResponse queryName="Trade History" type="AF"><FlexStatements count="1">'
    "</FlexStatements></FlexQueryResponse>"
)
GET_FATAL = (
    "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1009</ErrorCode>"
    "<ErrorMessage>Account does not have permission.</ErrorMessage></FlexStatementResponse>"
)


class _FakeHTTP:
    """Routes by endpoint and pops one queued response per call."""

    def __init__(self, send_seq, get_seq):
        self.send_seq = list(send_seq)
        self.get_seq = list(get_seq)
        self.send_calls = 0
        self.get_calls = 0

    def __call__(self, url):
        if "SendRequest" in url:
            self.send_calls += 1
            return self.send_seq.pop(0)
        if "GetStatement" in url:
            self.get_calls += 1
            return self.get_seq.pop(0)
        raise AssertionError(f"unexpected url: {url}")


def _no_sleep(_):
    pass


def test_happy_path_single_shot():
    http = _FakeHTTP([SEND_OK], [GET_OK])
    body = fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)
    assert "<Trade " in body and "IWM" in body
    assert http.send_calls == 1 and http.get_calls == 1


def test_send_throttle_then_success():
    http = _FakeHTTP([SEND_THROTTLE, SEND_THROTTLE, SEND_OK], [GET_OK])
    slept = []
    body = fetch_flex_statement(
        "tok", "qid", http_get=http, sleep=slept.append, send_backoff_s=7.0
    )
    assert "IWM" in body
    assert http.send_calls == 3
    assert slept == [7.0, 7.0]  # backed off before each retry, not after the success


def test_send_throttle_exhausted_raises():
    http = _FakeHTTP([SEND_THROTTLE] * 3, [])
    with pytest.raises(FlexError, match="throttled"):
        fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep, send_attempts=3)
    assert http.send_calls == 3


def test_send_fatal_error_raises_immediately():
    http = _FakeHTTP([SEND_FATAL], [])
    with pytest.raises(FlexError, match="1003"):
        fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)
    assert http.send_calls == 1  # no retry on a non-1001 error


def test_get_in_progress_then_ready():
    http = _FakeHTTP([SEND_OK], [GET_INPROGRESS, GET_INPROGRESS, GET_OK])
    body = fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)
    assert "IWM" in body
    assert http.get_calls == 3


def test_get_in_progress_exhausted_raises():
    http = _FakeHTTP([SEND_OK], [GET_INPROGRESS] * 4)
    with pytest.raises(FlexError, match="still generating"):
        fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep, get_attempts=4)
    assert http.get_calls == 4


def test_get_fatal_error_raises():
    http = _FakeHTTP([SEND_OK], [GET_FATAL])
    with pytest.raises(FlexError, match="1009"):
        fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)


def test_empty_but_valid_statement_returns_body():
    # A window with no trades is a delivered statement, not an error/in-progress.
    http = _FakeHTTP([SEND_OK], [GET_OK_EMPTY])
    body = fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)
    assert "FlexQueryResponse" in body
    assert http.get_calls == 1  # not polled as if still generating


@pytest.mark.parametrize("token,qid", [("", "qid"), ("tok", ""), ("", "")])
def test_missing_credentials_raises(token, qid):
    with pytest.raises(FlexError, match="required"):
        fetch_flex_statement(token, qid, http_get=lambda u: SEND_OK, sleep=_no_sleep)


def test_default_http_get_reraises_transport_error_as_flexerror(monkeypatch):
    # A DNS / transport failure (e.g. socket.gaierror "[Errno 11001] getaddrinfo
    # failed") raises raw URLError; after exhausting in-process retries
    # _default_http_get must funnel it through FlexError so reconcile_flex.py's
    # `except FlexError` graceful path engages instead of an uncaught traceback +
    # non-zero exit.  Inject a no-op sleep so the backoff doesn't slow the test.
    calls = {"n": 0}

    def _boom(_req, timeout=30):
        calls["n"] += 1
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr("data.flex_client.urllib.request.urlopen", _boom)
    with pytest.raises(FlexError, match="Flex HTTP request failed"):
        _default_http_get("https://example.invalid/x", sleep=lambda _s: None)
    # 1 initial attempt + 2 retries (transport_retries default)
    assert calls["n"] == 3


def test_default_http_get_retries_transient_then_succeeds(monkeypatch):
    # A transient DNS blip on the first lookup that clears on retry must NOT abort
    # the fetch — the in-process retry recovers within the same run (the 6/15 +
    # 6/18 pre-market getaddrinfo failures).
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<FlexQueryResponse>ok</FlexQueryResponse>"

    calls = {"n": 0}

    def _flaky(_req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("getaddrinfo failed")
        return _Resp()

    slept = []
    monkeypatch.setattr("data.flex_client.urllib.request.urlopen", _flaky)
    body = _default_http_get("https://example.invalid/x", sleep=slept.append)
    assert "FlexQueryResponse" in body
    assert calls["n"] == 2          # failed once, succeeded on retry
    assert slept == [4.0]           # one backoff before the successful retry


def test_send_url_used_when_no_url_tag():
    # SendRequest success without a <Url> tag → fall back to the default GetStatement base.
    send_no_url = (
        "<FlexStatementResponse><Status>Success</Status>"
        "<ReferenceCode>111</ReferenceCode></FlexStatementResponse>"
    )
    http = _FakeHTTP([send_no_url], [GET_OK])
    body = fetch_flex_statement("tok", "qid", http_get=http, sleep=_no_sleep)
    assert "IWM" in body
    assert http.get_calls == 1
