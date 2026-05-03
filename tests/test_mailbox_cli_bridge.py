"""Tests for the MailboxBackedClient → ClaudeCliTransport bridge.

The bridge unblocks the legacy stocks debates (scout / hold / entry /
unblock / risk) when no Anthropic API key is set — they ride the
Claude subscription via ``shared.llm_transport`` instead. Every legacy
caller still uses the AnthropicClient-shaped API, so nothing else
changes.
"""
from __future__ import annotations

import json
from unittest.mock import patch
from typing import Any, Dict

import pytest


def _make_cli_transport_response(
    *, text: str = "{}", parsed: Dict[str, Any] | None = None,
):
    """Build an LlmResponse like ClaudeCliTransport.complete_structured returns."""
    from trading_bot.shared.llm_transport import LlmResponse
    return LlmResponse(
        text=text, input_tokens=10, output_tokens=20,
        model="claude-opus-4-7", request_id="rid-1",
        raw={"result": parsed} if parsed is not None else {},
    )


# ---------------------------------------------------------------------------
# Construction: when no API key, use the CLI bridge
# ---------------------------------------------------------------------------


def test_default_uses_cli_bridge(monkeypatch):
    """Operator-stated plan: subscription via CLI is the default for
    every role. MailboxBackedClient should construct a CLI-backed inner
    client out of the box, regardless of whether an API key is set."""
    monkeypatch.delenv("TRADING_BOT_LLM_PREFER_API", raising=False)
    monkeypatch.delenv("TRADING_BOT_MAILBOX_ENABLED", raising=False)

    # Stub the CLI transport so no real subprocess fires.
    from trading_bot.shared import llm_transport as transport_mod

    class _StubTransport:
        def __init__(self): self.calls = []
        def complete(self, **kw):
            self.calls.append(("complete", kw))
            return _make_cli_transport_response(text="hello")
        def complete_structured(self, **kw):
            self.calls.append(("structured", kw))
            return _make_cli_transport_response(parsed={"verdict": "elevate"})

    stub = _StubTransport()
    monkeypatch.setattr(transport_mod, "get_transport", lambda **kw: stub)

    from trading_bot.mailbox_backed_client import MailboxBackedClient
    client = MailboxBackedClient(role_name="scout_debate", model="opus-4", engine=None)
    # Inner client is the CLI bridge, not AnthropicClient
    assert client._inner.__class__.__name__ == "_CliFallbackClient"


def test_complete_routes_through_cli(monkeypatch):
    """A .complete() call should hit ClaudeCliTransport.complete and
    return a legacy-shaped AnthropicResponse."""
    monkeypatch.delenv("TRADING_BOT_LLM_PREFER_API", raising=False)
    from trading_bot.shared import llm_transport as transport_mod

    class _StubTransport:
        def complete(self, **kw):
            return _make_cli_transport_response(text="hi from cli")
        def complete_structured(self, **kw):
            raise AssertionError("should not be called")

    monkeypatch.setattr(transport_mod, "get_transport", lambda **kw: _StubTransport())

    from trading_bot.mailbox_backed_client import MailboxBackedClient
    client = MailboxBackedClient(role_name="r", model="m", engine=None)
    out = client.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}], max_tokens=100,
    )
    assert out.text == "hi from cli"
    assert out.input_tokens == 10
    assert out.output_tokens == 20


def test_complete_structured_translates_tool_schema_to_json_schema(monkeypatch):
    """Legacy callers pass tool_schema; the bridge translates that into
    json_schema for the CLI transport, then unwraps the JSON response
    back into a StructuredResponse with .data populated."""
    monkeypatch.delenv("TRADING_BOT_LLM_PREFER_API", raising=False)
    captured = {}
    from trading_bot.shared import llm_transport as transport_mod

    class _StubTransport:
        def complete(self, **kw):
            raise AssertionError("should not be called")
        def complete_structured(self, **kw):
            captured["json_schema"] = kw["json_schema"]
            return _make_cli_transport_response(
                parsed={"symbol": "AAPL", "verdict": "elevate"},
            )

    monkeypatch.setattr(transport_mod, "get_transport", lambda **kw: _StubTransport())

    from trading_bot.mailbox_backed_client import MailboxBackedClient
    client = MailboxBackedClient(role_name="scout_judge", model="opus-4", engine=None)

    schema = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "verdict": {"type": "string", "enum": ["elevate", "dismiss"]},
        },
        "required": ["symbol", "verdict"],
    }
    out = client.complete_structured(
        system="judge", messages=[{"role": "user", "content": "decide"}],
        tool_name="cast_scout_verdict",
        tool_description="cast verdict",
        tool_schema=schema,
        max_tokens=500,
    )
    assert out.used_structured is True
    assert out.data == {"symbol": "AAPL", "verdict": "elevate"}
    # Contract: tool_schema reaches the CLI as json_schema unchanged.
    assert captured["json_schema"] == schema


def test_complete_structured_falls_back_to_text_parsing(monkeypatch):
    """When raw['result'] is empty but text contains valid JSON, the
    bridge parses .text and still surfaces .data."""
    monkeypatch.delenv("TRADING_BOT_LLM_PREFER_API", raising=False)
    from trading_bot.shared import llm_transport as transport_mod
    from trading_bot.shared.llm_transport import LlmResponse

    class _StubTransport:
        def complete(self, **kw): pass
        def complete_structured(self, **kw):
            return LlmResponse(
                text='{"a": 1}', input_tokens=0, output_tokens=0,
                model="m", request_id=None, raw={},
            )

    monkeypatch.setattr(transport_mod, "get_transport", lambda **kw: _StubTransport())

    from trading_bot.mailbox_backed_client import MailboxBackedClient
    client = MailboxBackedClient(role_name="r", model="m", engine=None)
    out = client.complete_structured(
        system="x", messages=[{"role":"user","content":"y"}],
        tool_name="t", tool_description="d", tool_schema={"type":"object"},
    )
    assert out.data == {"a": 1}
    assert out.used_structured is True


def test_prefer_api_opts_into_anthropic_path(monkeypatch):
    """The opt-in PREFER_API env var routes through the legacy
    AnthropicClient when an API key is set — useful for live A/B."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("TRADING_BOT_LLM_PREFER_API", "1")
    monkeypatch.delenv("TRADING_BOT_MAILBOX_ENABLED", raising=False)

    # Anthropic SDK is imported lazily inside AnthropicClient.__init__
    # — patch the import path so it doesn't actually hit the network.
    import sys
    from types import SimpleNamespace, ModuleType
    fake_anthropic = ModuleType("anthropic")
    fake_anthropic.Anthropic = lambda **kw: SimpleNamespace()
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from trading_bot.mailbox_backed_client import MailboxBackedClient
    client = MailboxBackedClient(role_name="r", model="m", engine=None)
    assert client._inner.__class__.__name__ == "AnthropicClient"


def test_prefer_api_falls_back_to_cli_when_no_key(monkeypatch):
    """PREFER_API=1 with no ANTHROPIC_API_KEY → fall back to CLI rather
    than crash."""
    monkeypatch.setenv("TRADING_BOT_LLM_PREFER_API", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Also strip the dotenv-loaded value (load_dotenv runs at module
    # import time and won't override existing env, but if a prior test
    # populated it via setenv it'll still be live in os.environ).
    if "ANTHROPIC_API_KEY" in __import__("os").environ:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from trading_bot.shared import llm_transport as transport_mod

    class _StubTransport:
        def complete(self, **kw): pass
        def complete_structured(self, **kw): pass

    monkeypatch.setattr(transport_mod, "get_transport", lambda **kw: _StubTransport())

    # Force AnthropicClient to raise so the bridge falls through.
    with patch(
        "trading_bot.mailbox_backed_client.AnthropicClient",
        side_effect=__import__(
            "trading_bot.anthropic_client", fromlist=["AnthropicCredsMissingError"]
        ).AnthropicCredsMissingError("no key"),
    ):
        from trading_bot.mailbox_backed_client import MailboxBackedClient
        client = MailboxBackedClient(role_name="r", model="m", engine=None)
    assert client._inner.__class__.__name__ == "_CliFallbackClient"
