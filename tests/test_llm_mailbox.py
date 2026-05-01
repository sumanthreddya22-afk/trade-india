"""Tests for trading_bot.llm_mailbox + trading_bot.mailbox_backed_client.

The mailbox is filesystem-only. The MailboxBackedClient is a thin
wrapper that prefers the mailbox transport and falls back to the inner
AnthropicClient on timeout / disabled / error.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.llm_mailbox import Brief, MailboxQueue, Result
from trading_bot.mailbox_backed_client import (
    MailboxBackedClient,
    MailboxRouting,
)
from trading_bot.anthropic_client import (
    AnthropicResponse,
    StructuredResponse,
)


# ---------------------------------------------------------------------------
# MailboxQueue
# ---------------------------------------------------------------------------


def test_submit_creates_pending_file_with_correct_schema(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    brief = Brief(
        role="decision_reflector", model_class="reflector",
        system="be terse", messages=[{"role": "user", "content": "why?"}],
        max_tokens=100, deadline_seconds=60,
    )
    bid = mb.submit(brief)

    assert mb.stats() == {"pending": 1, "done": 0, "processed": 0, "failed": 0}
    pending = mb.list_pending_briefs()
    assert len(pending) == 1
    p = pending[0]
    assert p["id"] == bid
    assert p["role"] == "decision_reflector"
    assert p["model_class"] == "reflector"
    assert p["system"] == "be terse"
    assert p["messages"] == [{"role": "user", "content": "why?"}]
    assert "submitted_at_utc" in p
    assert "deadline_utc" in p
    assert "tool" not in p  # no tool was supplied


def test_submit_with_tool_includes_schema(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    brief = Brief(
        role="r", model_class="reflector", system="s",
        messages=[{"role": "user", "content": "u"}],
        tool_name="record_lesson",
        tool_description="post-mortem",
        tool_schema={"type": "object", "properties": {"lesson": {"type": "string"}}},
    )
    mb.submit(brief)
    p = mb.list_pending_briefs()[0]
    assert p["tool"]["name"] == "record_lesson"
    assert p["tool"]["description"] == "post-mortem"
    assert p["tool"]["schema"]["type"] == "object"


def test_write_result_moves_brief_to_processed(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))
    assert mb.stats()["pending"] == 1

    mb.write_result(bid, result={
        "id": bid,
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "text": "hi", "structured": None,
        "model_used": "claude-opus-4-7", "input_tokens": None,
        "output_tokens": None, "error": None,
    })
    assert mb.stats() == {"pending": 0, "done": 1, "processed": 1, "failed": 0}


def test_poll_returns_result_after_routine_writes(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))

    mb.write_result(bid, result={
        "id": bid, "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "text": "answer", "structured": {"key": "val"},
        "model_used": "claude-opus-4-7",
        "input_tokens": 10, "output_tokens": 20, "error": None,
    })

    res = mb.poll(bid, timeout_seconds=5)
    assert res is not None
    assert res.text == "answer"
    assert res.structured == {"key": "val"}
    assert res.used_structured is True
    assert res.input_tokens == 10
    assert res.output_tokens == 20
    assert res.error is None
    # After consume, done/ should be empty (moved to processed/).
    assert mb.stats()["done"] == 0


def test_poll_returns_none_on_timeout(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))
    res = mb.poll(bid, timeout_seconds=0.5)
    assert res is None


def test_poll_succeeds_when_result_arrives_during_wait(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))

    def write_after_delay():
        time.sleep(1.5)
        mb.write_result(bid, result={
            "id": bid,
            "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "text": "late but ok", "structured": None,
            "model_used": "x", "input_tokens": None, "output_tokens": None,
            "error": None,
        })

    t = threading.Thread(target=write_after_delay, daemon=True)
    t.start()
    res = mb.poll(bid, timeout_seconds=5)
    t.join()

    assert res is not None
    assert res.text == "late but ok"


def test_corrupt_result_moves_to_failed_returns_none(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))
    # Hand-craft a corrupt done/<bid>.json
    (tmp_path / "done" / f"{bid}.json").write_text("{ not json")
    res = mb.poll(bid, timeout_seconds=1)
    assert res is None
    assert mb.stats()["failed"] == 1


def test_unparseable_pending_brief_moves_to_failed(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    (tmp_path / "pending" / "bad.json").write_text("{not json")
    pending = mb.list_pending_briefs()
    assert pending == []
    assert mb.stats()["failed"] == 1


def test_cleanup_old_removes_expired_files(tmp_path):
    mb = MailboxQueue(base=tmp_path)
    bid = mb.submit(Brief(role="r", model_class="reflector", system="s",
                          messages=[{"role": "user", "content": "u"}]))
    mb.write_result(bid, result={
        "id": bid, "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "text": "x", "structured": None, "model_used": "x",
        "input_tokens": None, "output_tokens": None, "error": None,
    })
    # Backdate the processed file by 30 days
    proc = next((tmp_path / "processed").glob("*.json"))
    old = time.time() - 30 * 86400
    import os
    os.utime(proc, (old, old))
    n = mb.cleanup_old(days=14)
    assert n == 1
    assert mb.stats()["processed"] == 0


# ---------------------------------------------------------------------------
# MailboxBackedClient
# ---------------------------------------------------------------------------


def _routing(enabled: bool, timeout: float = 0.3) -> MailboxRouting:
    return MailboxRouting(enabled=enabled, timeout_seconds=timeout, model_class="reflector")


def test_disabled_routing_falls_through_to_inner(tmp_path):
    """When enabled=False, the wrapper is a transparent passthrough."""
    fake_engine = MagicMock()
    inner_resp = AnthropicResponse(text="api answer", input_tokens=1,
                                   output_tokens=2, request_id="req-1",
                                   model="claude-opus-4-7")

    with patch("trading_bot.mailbox_backed_client.AnthropicClient") as mock_cls:
        mock_cls.return_value.complete.return_value = inner_resp
        client = MailboxBackedClient(
            role_name="r", model="claude-opus-4-7", engine=fake_engine,
            routing=_routing(enabled=False),
            mailbox=MailboxQueue(base=tmp_path),
        )
        out = client.complete(system="s", messages=[{"role": "user", "content": "q"}])

    assert out.text == "api answer"
    assert out.model == "claude-opus-4-7"
    # Mailbox dir was created but no briefs flowed through it.
    assert MailboxQueue(base=tmp_path).stats()["pending"] == 0


def test_enabled_routing_falls_back_on_timeout(tmp_path):
    """When mailbox times out, the wrapper calls the API and returns that."""
    fake_engine = MagicMock()
    inner_resp = AnthropicResponse(text="api fallback", input_tokens=1,
                                   output_tokens=2, request_id="req-2",
                                   model="claude-opus-4-7")

    with patch("trading_bot.mailbox_backed_client.AnthropicClient") as mock_cls:
        mock_cls.return_value.complete.return_value = inner_resp
        client = MailboxBackedClient(
            role_name="r", model="claude-opus-4-7", engine=fake_engine,
            routing=_routing(enabled=True, timeout=0.3),
            mailbox=MailboxQueue(base=tmp_path),
        )
        out = client.complete(system="s", messages=[{"role": "user", "content": "q"}])

    assert out.text == "api fallback"
    # Brief WAS submitted (and stays in pending/, since no routine processed it).
    assert MailboxQueue(base=tmp_path).stats()["pending"] == 1


def test_enabled_routing_returns_mailbox_result_when_present(tmp_path):
    """When the routine writes a result before timeout, the wrapper returns
    it without calling the inner API client."""
    fake_engine = MagicMock()
    mb = MailboxQueue(base=tmp_path)

    with patch("trading_bot.mailbox_backed_client.AnthropicClient") as mock_cls:
        mock_cls.return_value.complete.side_effect = AssertionError(
            "API client must NOT be called when mailbox returns a result"
        )

        # Pre-stage a result that WILL appear during the poll window.
        def write_result_async():
            time.sleep(0.5)
            # Read the just-submitted brief id.
            pending = mb.list_pending_briefs()
            bid = pending[0]["id"]
            mb.write_result(bid, result={
                "id": bid,
                "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "text": "subscription answer",
                "structured": None,
                "model_used": "claude-opus-4-7",
                "input_tokens": 10, "output_tokens": 20, "error": None,
            })

        t = threading.Thread(target=write_result_async, daemon=True)
        t.start()

        client = MailboxBackedClient(
            role_name="r", model="claude-opus-4-7", engine=fake_engine,
            routing=_routing(enabled=True, timeout=5.0),
            mailbox=mb,
        )
        out = client.complete(system="s", messages=[{"role": "user", "content": "q"}])
        t.join()

    assert out.text == "subscription answer"
    assert out.model == "claude-opus-4-7"


def test_complete_structured_returns_structured_result(tmp_path):
    """The structured path packs the routine's structured output into
    StructuredResponse with used_structured=True."""
    fake_engine = MagicMock()
    mb = MailboxQueue(base=tmp_path)

    with patch("trading_bot.mailbox_backed_client.AnthropicClient") as mock_cls:
        mock_cls.return_value.complete_structured.side_effect = AssertionError(
            "API client must NOT be called when mailbox returns a structured result"
        )

        def write_result_async():
            time.sleep(0.3)
            pending = mb.list_pending_briefs()
            bid = pending[0]["id"]
            mb.write_result(bid, result={
                "id": bid,
                "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "text": "",
                "structured": {"lesson": "stop too tight", "tags": ["risk_mgmt"]},
                "model_used": "claude-opus-4-7",
                "input_tokens": None, "output_tokens": None, "error": None,
            })

        t = threading.Thread(target=write_result_async, daemon=True)
        t.start()

        client = MailboxBackedClient(
            role_name="r", model="claude-opus-4-7", engine=fake_engine,
            routing=_routing(enabled=True, timeout=5.0),
            mailbox=mb,
        )
        out = client.complete_structured(
            system="s", messages=[{"role": "user", "content": "q"}],
            tool_name="record_lesson", tool_description="post-mortem",
            tool_schema={"type": "object"},
        )
        t.join()

    assert out.used_structured is True
    assert out.data == {"lesson": "stop too tight", "tags": ["risk_mgmt"]}
    assert out.model == "claude-opus-4-7"


def test_routine_error_in_result_falls_back_to_api(tmp_path):
    """If the routine writes a result with error!=null, the wrapper falls
    back to direct API rather than returning the broken result."""
    fake_engine = MagicMock()
    mb = MailboxQueue(base=tmp_path)
    inner_resp = AnthropicResponse(text="api after routine error", input_tokens=1,
                                   output_tokens=2, request_id="req-3",
                                   model="claude-opus-4-7")

    with patch("trading_bot.mailbox_backed_client.AnthropicClient") as mock_cls:
        mock_cls.return_value.complete.return_value = inner_resp

        def write_error_result_async():
            time.sleep(0.2)
            pending = mb.list_pending_briefs()
            bid = pending[0]["id"]
            mb.write_result(bid, result={
                "id": bid,
                "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "text": "", "structured": None,
                "model_used": "claude-opus-4-7",
                "input_tokens": None, "output_tokens": None,
                "error": "rate_limit_during_routine",
            })

        t = threading.Thread(target=write_error_result_async, daemon=True)
        t.start()

        client = MailboxBackedClient(
            role_name="r", model="claude-opus-4-7", engine=fake_engine,
            routing=_routing(enabled=True, timeout=5.0),
            mailbox=mb,
        )
        out = client.complete(system="s", messages=[{"role": "user", "content": "q"}])
        t.join()

    assert out.text == "api after routine error"
