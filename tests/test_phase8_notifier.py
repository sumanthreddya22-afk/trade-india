"""Email notifier: graceful no-op when creds missing; dedup in-process."""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.obs import notifier


def test_send_alert_noop_without_creds(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    out = notifier.send_alert(subject="x", body="y")
    assert out == {"ok": False, "reason": "creds_missing"}


def test_dedup_window_blocks_repeat_subject(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake")
    monkeypatch.setenv("TRADING_BOT_ALERT_TO", "to@y.com")

    # Reset module state.
    notifier._recent_sends.clear()
    sends = []

    class _FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a, **kw): pass
        def send_message(self, msg): sends.append(msg["Subject"])
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    r1 = notifier.send_alert(subject="halt", body="x", severity="WARN",
                              dedup_key="k1")
    r2 = notifier.send_alert(subject="halt", body="x", severity="WARN",
                              dedup_key="k1")
    assert r1["ok"] and r1.get("deduplicated") is not True
    assert r2["ok"] and r2.get("deduplicated") is True
    assert len(sends) == 1


def test_kill_switch_alert_format(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "x@y.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake")
    notifier._recent_sends.clear()

    captured = []

    class _FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a, **kw): pass
        def send_message(self, msg):
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            captured.append((msg["Subject"], payload))
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)

    notifier.send_kill_switch_alert(
        detector="recon_mismatch", reason="match=0", actor="system",
    )
    assert captured
    subj, body = captured[0]
    assert "KILL SWITCH FIRED" in subj
    assert "recon_mismatch" in subj
    assert "recon_mismatch" in body
    assert "match=0" in body
