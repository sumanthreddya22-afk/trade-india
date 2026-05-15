"""Phase 12 — LLM transport + throttle + ledger row tests.

Covers:
- ROLE_MODEL maps every persona to a real model.
- llm_throttle.acquire() returns the right verdict for each priority.
- Cache get/put round-trips and respects TTL.
- llm_call_event writes hash-chained rows.
- invoke() does cache hits without re-spending budget.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from trading_bot.ledger.hash_chain import verify_chain
from trading_bot.ledger.llm_call_event import calls_today, write_event
from trading_bot.shared import llm_transport, llm_throttle


# ---- ROLE_MODEL -----------------------------------------------------------

def test_role_model_keys_have_sonnet_or_opus() -> None:
    for role, model in llm_transport.ROLE_MODEL.items():
        assert model in {"sonnet", "opus"}, f"{role} -> {model!r}"


def test_resolve_model_falls_back_to_sonnet() -> None:
    assert llm_transport.resolve_model("unknown_role") == "sonnet"


def test_resolve_model_override_wins() -> None:
    assert llm_transport.resolve_model("regime_analyst", "opus") == "opus"


def test_resolve_priority_falls_back_to_p2() -> None:
    assert llm_transport.resolve_priority("unknown_role") == "P2"


def test_resolve_priority_known_roles() -> None:
    assert llm_transport.resolve_priority("regime_analyst") == "P0"
    assert llm_transport.resolve_priority("scout_summarizer") == "P3"


# ---- Throttle -------------------------------------------------------------

class _FakeConn:
    def __init__(self, used_today: int) -> None:
        self._used = used_today

    def execute(self, sql: str, params: Any = ()) -> "_FakeConn":
        return self

    def fetchone(self) -> tuple:
        return (self._used,)


def test_acquire_p0_never_blocks(monkeypatch) -> None:
    monkeypatch.setattr(llm_throttle, "calls_today", lambda conn: 10_000)
    d = llm_throttle.acquire(persona_id="regime_analyst", priority="P0", conn=object())
    assert d.verdict == "proceed"


def test_acquire_p1_defers_at_80(monkeypatch) -> None:
    monkeypatch.setattr(llm_throttle, "daily_cap", lambda: 100)
    monkeypatch.setattr(llm_throttle, "calls_today", lambda conn: 85)
    d = llm_throttle.acquire(persona_id="drift_postmortem", priority="P1", conn=object())
    assert d.verdict == "defer"


def test_acquire_p3_drops_at_40(monkeypatch) -> None:
    monkeypatch.setattr(llm_throttle, "daily_cap", lambda: 100)
    monkeypatch.setattr(llm_throttle, "calls_today", lambda conn: 50)
    d = llm_throttle.acquire(persona_id="scout_summarizer", priority="P3", conn=object())
    assert d.verdict == "drop"


def test_acquire_p2_proceeds_below_60(monkeypatch) -> None:
    monkeypatch.setattr(llm_throttle, "daily_cap", lambda: 100)
    monkeypatch.setattr(llm_throttle, "calls_today", lambda conn: 30)
    d = llm_throttle.acquire(persona_id="mutation_reviewer", priority="P2", conn=object())
    assert d.verdict == "proceed"


# ---- Cache ----------------------------------------------------------------

def test_cache_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_BOT_LLM_CACHE_DIR", str(tmp_path))
    key = llm_throttle.input_hash("regime_analyst", "sonnet", "hello world")
    assert llm_throttle.cache_get("regime_analyst", key) is None
    llm_throttle.cache_put("regime_analyst", key, '{"result":"x"}')
    assert llm_throttle.cache_get("regime_analyst", key) == '{"result":"x"}'


def test_cache_expires_past_ttl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_BOT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setitem(llm_throttle._PERSONA_TTL, "scout_summarizer", 1)
    key = llm_throttle.input_hash("scout_summarizer", "sonnet", "x")
    llm_throttle.cache_put("scout_summarizer", key, '{"result":"x"}')
    time.sleep(1.2)
    assert llm_throttle.cache_get("scout_summarizer", key) is None


# ---- Ledger writer + chain ----------------------------------------------

def test_write_llm_call_event_chains(ledger_conn) -> None:
    seq1 = write_event(
        ledger_conn,
        persona_id="regime_analyst", model="sonnet", priority="P0",
        input_hash="a" * 64, cache_hit=False,
    )
    seq2 = write_event(
        ledger_conn,
        persona_id="regime_analyst", model="sonnet", priority="P0",
        input_hash="b" * 64, cache_hit=True,
    )
    assert seq2 > seq1
    assert verify_chain(ledger_conn, "llm_call_event") == 2


def test_calls_today_excludes_cache_hits(ledger_conn) -> None:
    write_event(ledger_conn, persona_id="r", model="sonnet", priority="P0",
                input_hash="a" * 64, cache_hit=False)
    write_event(ledger_conn, persona_id="r", model="sonnet", priority="P0",
                input_hash="b" * 64, cache_hit=True)
    write_event(ledger_conn, persona_id="r", model="sonnet", priority="P0",
                input_hash="c" * 64, cache_hit=False, dropped=True)
    assert calls_today(ledger_conn) == 1


# ---- invoke() with mocked spawn ------------------------------------------

def test_invoke_uses_cache_on_second_call(tmp_path, monkeypatch, ledger_conn) -> None:
    monkeypatch.setenv("TRADING_BOT_LLM_CACHE_DIR", str(tmp_path))
    fake_response = json.dumps({
        "type": "result", "result": "OK",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    })
    spawn_calls: list[str] = []

    def _fake_spawn(prompt, model, timeout_s):
        spawn_calls.append(prompt)
        return fake_response, 42

    monkeypatch.setattr(llm_transport, "_spawn_claude", _fake_spawn)

    r1 = llm_transport.invoke(
        role="scout_summarizer", prompt="hello", conn=ledger_conn,
    )
    r2 = llm_transport.invoke(
        role="scout_summarizer", prompt="hello", conn=ledger_conn,
    )
    assert r1.cache_hit is False
    assert r2.cache_hit is True
    assert len(spawn_calls) == 1  # second call used cache
    assert r1.text == "OK"
    assert r1.input_tokens == 7
    # Two ledger rows: one fresh, one cache-hit (both audited).
    assert calls_today(ledger_conn, exclude_cache_hits=False) == 2
    assert calls_today(ledger_conn, exclude_cache_hits=True) == 1


def test_invoke_dropped_when_p3_over_budget(tmp_path, monkeypatch, ledger_conn) -> None:
    monkeypatch.setenv("TRADING_BOT_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(llm_throttle, "daily_cap", lambda: 10)
    monkeypatch.setattr(llm_throttle, "calls_today", lambda conn: 5)

    def _fake_spawn(prompt, model, timeout_s):
        raise AssertionError("should not be called when dropped")

    monkeypatch.setattr(llm_transport, "_spawn_claude", _fake_spawn)

    with pytest.raises(llm_transport.LLMUnavailable):
        llm_transport.invoke(
            role="scout_summarizer", prompt="x", conn=ledger_conn,
        )
