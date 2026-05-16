"""WS5a — order router retry-with-backoff for transient broker errors."""
from __future__ import annotations

from trading_bot.execution.order_router import (
    RETRY_BACKOFFS_S,
    _is_transient,
    _submit_with_retry,
)


def test_classify_transient_errors() -> None:
    assert _is_transient("timeout") is True
    assert _is_transient("connection reset by peer") is True
    assert _is_transient("HTTP 502 Bad Gateway") is True
    assert _is_transient("Rate limit exceeded") is True
    assert _is_transient("Service Unavailable") is True


def test_classify_permanent_errors() -> None:
    assert _is_transient("REJECTED: bad symbol") is False
    assert _is_transient("INSUFFICIENT_BUYING_POWER") is False
    assert _is_transient("market closed") is False
    assert _is_transient("PDT rule blocked") is False
    assert _is_transient("shadow_mode (ENABLE_SUBMIT=false)") is False
    # Default: unknown error -> NOT transient (fail closed).
    assert _is_transient("something weird") is False
    assert _is_transient("") is False


def test_retry_succeeds_after_transient_then_success() -> None:
    calls: list[int] = []

    def broker(**kw):
        calls.append(1)
        if len(calls) <= 2:
            return {"ok": False, "broker_order_id": None, "error": "timeout"}
        return {"ok": True, "broker_order_id": "B1", "status": "accepted"}

    sleeps: list[float] = []
    r = _submit_with_retry(broker, {"a": 1}, sleep=sleeps.append)
    assert r["ok"] is True
    assert r["broker_order_id"] == "B1"
    assert len(calls) == 3
    # Two retries -> two sleeps with the configured backoffs.
    assert sleeps == [RETRY_BACKOFFS_S[0], RETRY_BACKOFFS_S[1]]


def test_retry_gives_up_after_exhausting_backoffs() -> None:
    calls: list[int] = []

    def broker(**kw):
        calls.append(1)
        return {"ok": False, "broker_order_id": None, "error": "HTTP 503"}

    sleeps: list[float] = []
    r = _submit_with_retry(broker, {"a": 1}, sleep=sleeps.append)
    assert r["ok"] is False
    # Initial attempt + 3 retries = 4 total attempts.
    assert len(calls) == 1 + len(RETRY_BACKOFFS_S)
    assert sleeps == list(RETRY_BACKOFFS_S)


def test_permanent_errors_skip_retry() -> None:
    calls: list[int] = []

    def broker(**kw):
        calls.append(1)
        return {"ok": False, "broker_order_id": None, "error": "REJECTED: bad order"}

    sleeps: list[float] = []
    r = _submit_with_retry(broker, {}, sleep=sleeps.append)
    assert r["ok"] is False
    assert len(calls) == 1
    assert sleeps == []


def test_exception_treated_as_error_string() -> None:
    """Broker raising an exception is captured into the same dict shape
    and classified by message content. A bare RuntimeError isn't
    transient by default, so we give up on the first attempt."""
    calls: list[int] = []

    def broker(**kw):
        calls.append(1)
        raise RuntimeError("connection reset")

    sleeps: list[float] = []
    r = _submit_with_retry(broker, {}, sleep=sleeps.append)
    assert r["ok"] is False
    # "connection reset" is transient -> retries exhaust then return.
    assert len(calls) == 1 + len(RETRY_BACKOFFS_S)


def test_exception_permanent_not_retried() -> None:
    calls: list[int] = []

    def broker(**kw):
        calls.append(1)
        raise RuntimeError("not a transient kind of failure")

    r = _submit_with_retry(broker, {}, sleep=lambda _s: None)
    assert r["ok"] is False
    assert len(calls) == 1
