# tests/test_email_digest_with_report_cards.py
import datetime as dt
from decimal import Decimal
from trading_bot.email_digest import DigestContext, build_digest_email
from trading_bot.roles.base import ReportCard, HealthStatus


def test_digest_with_report_cards():
    cards = [
        ReportCard(role_name="stock_scanner", period_days=30,
                   kpi_name="buy_win_rate_5d", kpi_value=0.62,
                   summary="62% win rate", health=HealthStatus.OK),
        ReportCard(role_name="account_sentinel", period_days=30,
                   kpi_name="current_drawdown_pct", kpi_value=2.4,
                   summary="2.4% drawdown", health=HealthStatus.OK),
    ]
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500"),
        ending_equity=Decimal("103895"),
        realized_pnl=Decimal("-422"),
        unrealized_pnl=Decimal("139"),
        regime="trending_up",
        active_config_version="phase2-v1",
        trades=[],
        errors=[],
        role_report_cards=cards,
    )
    email = build_digest_email(ctx)
    assert "Role Report Cards" in email.html_body or "report card" in email.html_body.lower()
    assert "stock_scanner" in email.html_body
    assert "62%" in email.html_body or "0.62" in email.html_body


def test_digest_zero_starting_equity_does_not_crash():
    ctx = DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("0"),
        ending_equity=Decimal("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="sideways",
        active_config_version="phase2-v1",
        trades=[],
        errors=[],
    )
    email = build_digest_email(ctx)  # must not raise DivisionByZero
    assert "0.00%" in email.subject or "0%" in email.subject
