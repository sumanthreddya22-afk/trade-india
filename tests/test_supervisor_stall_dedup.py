"""Tests for supervisor's stall-alert dedupe (A6). The daemon may stall
briefly during DB migrations, lab promotion swaps, etc. If the watchdog
auto-recovers the daemon within 60s, we suppress the CRITICAL email and
log a daemon_blip_recovered event instead."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_quick_recovery_suppresses_alert_email():
    """Stall + kickstart success + heartbeat fresh within 60s → no email."""
    from trading_bot.supervisor import _handle_stall

    log = MagicMock()
    send_alert = MagicMock()
    is_heartbeat_fresh = MagicMock(return_value=True)  # daemon recovered
    sleep = MagicMock()  # don't actually sleep in test

    _handle_stall(
        log=log,
        age_seconds=305.0,
        kickstart_succeeded=True,
        send_alert=send_alert,
        is_heartbeat_fresh=is_heartbeat_fresh,
        sleep=sleep,
    )

    # Slept ~60s for recovery window
    sleep.assert_called_once()
    # Suppressed email
    send_alert.assert_not_called()
    # Logged the blip
    log.event.assert_any_call(
        "daemon_blip_recovered",
        stall_duration_seconds=305.0,
        recovery_method="kickstart",
    )


def test_kickstart_failed_still_sends_alert():
    """Stall + kickstart failed → email immediately, don't wait."""
    from trading_bot.supervisor import _handle_stall

    log = MagicMock()
    send_alert = MagicMock()
    is_heartbeat_fresh = MagicMock(return_value=False)
    sleep = MagicMock()

    _handle_stall(
        log=log,
        age_seconds=320.0,
        kickstart_succeeded=False,
        send_alert=send_alert,
        is_heartbeat_fresh=is_heartbeat_fresh,
        sleep=sleep,
    )

    send_alert.assert_called_once()
    # Email subject should mention "Daemon stalled" (existing format).
    args, kwargs = send_alert.call_args
    assert "Daemon stalled" in kwargs.get("subject", "") or "Daemon stalled" in str(args)


def test_kickstart_succeeded_but_heartbeat_still_stale_sends_alert():
    """Stall + kickstart attempted + heartbeat still stale after 60s → email."""
    from trading_bot.supervisor import _handle_stall

    log = MagicMock()
    send_alert = MagicMock()
    is_heartbeat_fresh = MagicMock(return_value=False)  # still stale
    sleep = MagicMock()

    _handle_stall(
        log=log,
        age_seconds=400.0,
        kickstart_succeeded=True,
        send_alert=send_alert,
        is_heartbeat_fresh=is_heartbeat_fresh,
        sleep=sleep,
    )

    send_alert.assert_called_once()
