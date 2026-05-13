"""P0 acceptance: the live-param-writes feature flag works.

Plan v4 §15: "Auto-tuning / param mutation in live config — Delete now
(Phase 0)." The v4 cleanup deleted every caller of the old auto-tune
write paths (``threshold_overrides.write_override``, ``evolution.save_params``).
The gate they would have consulted lives on so future code can declare
intent the same way: any module that *would* mutate live config has to
check ``live_param_writes_allowed()`` first and either no-op or write to
a shadow/observation surface.
"""
from __future__ import annotations

import pytest

from trading_bot.feature_flags import (
    live_param_writes_allowed,
    log_live_write_blocked,
)


def _unblock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_BOT_ALLOW_LIVE_PARAM_WRITES", raising=False)


def test_default_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _unblock(monkeypatch)
    assert live_param_writes_allowed() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_truthy_env_unblocks(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TRADING_BOT_ALLOW_LIVE_PARAM_WRITES", value)
    assert live_param_writes_allowed() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_falsy_env_stays_blocked(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("TRADING_BOT_ALLOW_LIVE_PARAM_WRITES", value)
    assert live_param_writes_allowed() is False


def test_log_live_write_blocked_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="trading_bot.feature_flags"):
        log_live_write_blocked("future.module.fn", detail="knob=test")
    assert any("future.module.fn" in r.message for r in caplog.records)
    assert any("knob=test" in r.message for r in caplog.records)
