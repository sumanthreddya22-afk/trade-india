import datetime as dt
from decimal import Decimal
from trading_bot.email_digest import build_digest_email, DigestContext, TradeRow


def test_digest_subject_with_pnl_and_equity():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="v17",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "Apr 28" in email.subject
    assert "-0.58%" in email.subject or "-0.6%" in email.subject


def test_digest_body_includes_trades():
    trade = TradeRow(
        side="BUY", symbol="AAPL", qty=Decimal("41"),
        price=Decimal("190.24"), strategy="momentum_v3",
        time=dt.time(10, 0), status="open",
    )
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="v17",
        trades=[trade],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "AAPL" in email.html_body
    assert "190.24" in email.html_body


def test_digest_body_zero_trades():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("100000"),
        ending_equity=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="sideways",
        active_config_version="v17",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)
    assert "no trades" in email.html_body.lower() or "0 trades" in email.html_body.lower()


def test_digest_body_includes_errors():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("100000"),
        ending_equity=Decimal("100000"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="trending_up",
        active_config_version="v17",
        trades=[],
        errors=["14:23 — Polygon API timeout, auto-restarted"],
    )
    email = build_digest_email(ctx)
    assert "Polygon API timeout" in email.html_body
