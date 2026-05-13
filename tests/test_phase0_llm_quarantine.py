"""P0 acceptance: the LLM-hot-path feature flag works as documented.

Plan v4 §1A: LLM is allowed only in L3 (research) and L8 (postmortem).
Phase 0 ships a single gate, ``feature_flags.is_llm_hotpath_enabled()``,
that every L5 (kernel) and L6/L7 (risk + execution) call site MUST
consult before issuing an LLM call. The legacy ``*_debate.py`` modules
that previously embedded LLM in the hot path were deleted during the
v4 cleanup; the gate they consulted lives on for future hot-path code
to reference.
"""
from __future__ import annotations

import pytest

from trading_bot.feature_flags import (
    SkippedDebate,
    is_llm_hotpath_enabled,
    log_quarantine,
)


def _unset_hotpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_BOT_ENABLE_LLM_HOTPATH", raising=False)


def test_default_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_hotpath(monkeypatch)
    assert is_llm_hotpath_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "  yes "])
def test_truthy_env_enables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TRADING_BOT_ENABLE_LLM_HOTPATH", value)
    assert is_llm_hotpath_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "anything-else"])
def test_falsy_env_stays_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("TRADING_BOT_ENABLE_LLM_HOTPATH", value)
    assert is_llm_hotpath_enabled() is False


def test_skipped_debate_is_falsy() -> None:
    sd = SkippedDebate()
    assert bool(sd) is False
    assert sd.verdict == "skip"
    assert sd.reason == "hotpath_disabled"


def test_skipped_debate_carries_context() -> None:
    sd = SkippedDebate(reason="testing", pipeline="crypto_entry", symbol="BTCUSD")
    assert sd.pipeline == "crypto_entry"
    assert sd.symbol == "BTCUSD"


def test_log_quarantine_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    # Best-effort: the helper just logs; we assert it produces a record.
    import logging
    with caplog.at_level(logging.INFO, logger="trading_bot.feature_flags"):
        log_quarantine("future_kernel_path", symbol="SPY")
    assert any("future_kernel_path" in r.message for r in caplog.records)
