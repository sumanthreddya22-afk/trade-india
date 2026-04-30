"""AnthropicClient.complete_structured tests.

Mocks the Anthropic SDK's messages.create response to verify the two paths
downstream callers depend on: tool-use block → ``data`` populated, text-only
block → ``data=None`` (fallback).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from trading_bot.anthropic_client import AnthropicClient, StructuredResponse
from trading_bot.state_db import Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    return eng


def _fake_sdk_response(*, content_blocks, in_tokens=10, out_tokens=20, msg_id="msg-1"):
    return SimpleNamespace(
        content=content_blocks,
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
        id=msg_id,
    )


def test_complete_structured_extracts_tool_use_input(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    tool_block = SimpleNamespace(
        type="tool_use", name="propose", input={"proposals": [{"name": "x"}]}
    )
    sdk_resp = _fake_sdk_response(content_blocks=[tool_block])
    fake_sdk = MagicMock()
    fake_sdk.messages.create.return_value = sdk_resp

    client = AnthropicClient(role_name="t", model="claude-opus-4-7", engine=engine)
    client._client = fake_sdk

    out: StructuredResponse = client.complete_structured(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tool_name="propose",
        tool_description="d",
        tool_schema={"type": "object", "properties": {}},
    )
    assert out.used_structured is True
    assert out.data == {"proposals": [{"name": "x"}]}
    assert out.input_tokens == 10
    assert out.output_tokens == 20
    # Verify forced tool_choice was requested.
    call = fake_sdk.messages.create.call_args
    assert call.kwargs["tool_choice"] == {"type": "tool", "name": "propose"}
    assert call.kwargs["tools"][0]["name"] == "propose"


def test_complete_structured_falls_back_when_text_only(engine, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    text_block = SimpleNamespace(type="text", text="free-form output")
    sdk_resp = _fake_sdk_response(content_blocks=[text_block])
    fake_sdk = MagicMock()
    fake_sdk.messages.create.return_value = sdk_resp

    client = AnthropicClient(role_name="t", model="claude-opus-4-7", engine=engine)
    client._client = fake_sdk

    out = client.complete_structured(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tool_name="propose",
        tool_description="d",
        tool_schema={"type": "object"},
    )
    assert out.used_structured is False
    assert out.data is None
    assert out.text == "free-form output"


def test_complete_structured_ignores_wrong_tool_name(engine, monkeypatch):
    """If the SDK echoes a tool_use with a different name (shouldn't happen
    under tool_choice-forced use, but be defensive), data stays None."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE")
    wrong = SimpleNamespace(type="tool_use", name="other", input={"a": 1})
    sdk_resp = _fake_sdk_response(content_blocks=[wrong])
    fake_sdk = MagicMock()
    fake_sdk.messages.create.return_value = sdk_resp

    client = AnthropicClient(role_name="t", model="claude-opus-4-7", engine=engine)
    client._client = fake_sdk

    out = client.complete_structured(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tool_name="propose",
        tool_description="d",
        tool_schema={"type": "object"},
    )
    assert out.used_structured is False
    assert out.data is None
