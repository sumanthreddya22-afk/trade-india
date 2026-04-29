import datetime as dt


def test_promotion_email_renders_diff():
    from trading_bot.email_promotion import build_promotion_email
    promo = {
        "promoted_at": dt.datetime(2026, 4, 28, 10, 1, tzinfo=dt.timezone.utc),
        "version": "auto-20260428-100154",
        "template": "momentum",
        "git_sha": "abc1234",
        "fitness_at_promotion": 3.967,
        "params": {"rsi_lower": 50.07, "rsi_upper": 70.37, "stop_pct": 6.11},
        "risk_caps": {"daily_loss_pct": 3.0, "max_position_pct": 10.0},
    }
    prev = {
        "params": {"rsi_lower": 55.0, "rsi_upper": 70.0, "stop_pct": 5.0},
        "risk_caps": {"daily_loss_pct": 2.0, "max_position_pct": 10.0},
    }
    e = build_promotion_email(promo=promo, prev=prev)
    assert "auto-20260428-100154" in e.html_body
    assert "3.97" in e.html_body or "3.967" in e.html_body
    assert "rsi_lower" in e.html_body
    # Subject
    assert "Strategy Promoted" in e.subject


def test_promotion_email_no_prev_renders_all_as_new():
    from trading_bot.email_promotion import build_promotion_email
    promo = {
        "promoted_at": dt.datetime(2026, 4, 28, 10, 1, tzinfo=dt.timezone.utc),
        "version": "v1",
        "template": "momentum",
        "git_sha": "x",
        "fitness_at_promotion": 1.0,
        "params": {"rsi_lower": 55.0},
        "risk_caps": {},
    }
    e = build_promotion_email(promo=promo, prev=None)
    assert "rsi_lower" in e.html_body
