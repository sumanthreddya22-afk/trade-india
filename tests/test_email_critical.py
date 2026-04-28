from trading_bot.email_critical import build_critical_email


def test_critical_subject_has_prefix():
    email = build_critical_email(
        title="Daemon stalled",
        detail="No heartbeat in 5 min",
    )
    assert email.subject.startswith("[CRITICAL]")
    assert "Daemon stalled" in email.subject


def test_critical_body_includes_detail():
    email = build_critical_email(
        title="Drawdown breach",
        detail="20.4% from HWM $104,500. Pause flag written. Trading halted.",
    )
    assert "20.4%" in email.html_body
    assert "Pause flag written" in email.html_body


def test_critical_severity_high_marker():
    email = build_critical_email(title="X", detail="Y", severity="HIGH")
    assert "HIGH" in email.html_body
