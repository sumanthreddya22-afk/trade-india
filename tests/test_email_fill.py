from decimal import Decimal
from trading_bot.email_fill import build_fill_email, FillContext


def test_fill_email_subject():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    assert "BUY AAPL" in email.subject
    assert "190.24" in email.subject


def test_fill_email_body_contains_all_fields():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    body = email.html_body
    assert "AAPL" in body
    assert "41" in body
    assert "190.24" in body
    assert "180.69" in body
    assert "momentum_v3" in body
    assert "103,950" in body or "103950" in body


def test_fill_email_includes_slippage():
    ctx = FillContext(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        fill_price=Decimal("190.24"), expected_price=Decimal("190.20"),
        strategy="momentum_v3", stop_price=Decimal("180.69"),
        account_equity=Decimal("103950.00"),
    )
    email = build_fill_email(ctx)
    # Slippage = +$0.04 (worse for buyer; positive number)
    assert "0.04" in email.html_body


def test_fill_email_renders_for_option_csp_open():
    ctx = FillContext(
        side="option_csp_open", symbol="AAPL", qty=Decimal("1"),
        fill_price=Decimal("2.10"), expected_price=Decimal("2.10"),
        strategy="wheel", stop_price=None,
        account_equity=Decimal("100000"),
        contract="AAPL250516P00190000", strike=Decimal("190"),
        expiration="2025-05-16", notes="entry",
    )
    email = build_fill_email(ctx)
    assert "AAPL" in email.html_body
    assert "190" in email.html_body
    assert "AAPL250516P00190000" in email.html_body


def test_fill_email_renders_for_option_assignment():
    ctx = FillContext(
        side="option_assignment", symbol="AAPL", qty=Decimal("100"),
        fill_price=Decimal("190"), expected_price=Decimal("190"),
        strategy="wheel", stop_price=None,
        account_equity=Decimal("100000"),
        contract="AAPL250516P00190000", strike=Decimal("190"),
        expiration="2025-05-16",
        notes="assigned 100 shares @ 190",
    )
    email = build_fill_email(ctx)
    assert ("Assigned" in email.html_body
            or "assignment" in email.html_body.lower())


def test_stop_hit_subject_and_loss_amount():
    ctx = FillContext(
        side="STOP", symbol="BTC/USD", qty=Decimal("0.0935"),
        fill_price=Decimal("84920.00"), expected_price=Decimal("89440.00"),
        strategy="momentum_v3", stop_price=None,
        account_equity=Decimal("103527.38"), realized_pnl=Decimal("-422.62"),
    )
    email = build_fill_email(ctx)
    assert "STOP HIT" in email.subject
    assert "BTC/USD" in email.subject
    assert "-$422.62" in email.html_body or "-422.62" in email.html_body
