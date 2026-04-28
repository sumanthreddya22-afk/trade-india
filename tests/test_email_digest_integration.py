"""Integration test: daily digest builder output is consumable by EmailSender.

Task 19 — Phase 1 plan.

The plan's stub imported a non-existent `send_email` free function.  The
actual API is `EmailSender.send(subject=..., html_body=...)` with SMTP_SSL
under the hood.  We mock `smtplib.SMTP_SSL` so no network traffic is made
and assert that:
  1. `build_digest_email` produces a well-formed `Email` value object.
  2. `EmailSender.send` calls SMTP_SSL exactly once with the digest payload.
"""
import datetime as dt
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.email_digest import DigestContext, TradeRow, build_digest_email
from trading_bot.email_sender import EmailSender


def _make_ctx() -> DigestContext:
    return DigestContext(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("104500.00"),
        ending_equity=Decimal("103895.00"),
        realized_pnl=Decimal("-422.62"),
        unrealized_pnl=Decimal("139.72"),
        regime="trending_up",
        active_config_version="phase1-v1",
        trades=[
            TradeRow(
                side="BUY",
                symbol="AAPL",
                qty=Decimal("41"),
                price=Decimal("190.24"),
                strategy="momentum_v3",
                time=dt.time(10, 0),
                status="open",
            ),
        ],
        errors=[],
    )


def test_digest_email_is_well_formed():
    """build_digest_email returns an Email with subject and html_body."""
    email = build_digest_email(_make_ctx())

    assert email.subject, "subject must be non-empty"
    assert "Apr 28" in email.subject
    assert email.html_body, "html_body must be non-empty"
    # The body should contain the equity figures and trade row
    assert "AAPL" in email.html_body
    assert "momentum_v3" in email.html_body
    assert "trending_up" in email.html_body


def test_send_daily_digest_smtp_call():
    """Verify the digest builder + SMTP transport composition.

    Mocks smtplib.SMTP_SSL so no network call is made.
    EmailSender.send() must not raise and SMTP_SSL must be called exactly once.
    """
    email = build_digest_email(_make_ctx())

    with patch("trading_bot.email_sender.smtplib.SMTP_SSL") as MockSMTP:
        smtp_instance = MockSMTP.return_value.__enter__.return_value

        sender = EmailSender(
            user="bot@example.com",
            app_password="fake-app-password",
            to="bharath8887@gmail.com",
        )
        # send() returns None on success; if it raises the test fails
        sender.send(subject=email.subject, html_body=email.html_body)

        MockSMTP.assert_called_once()
        smtp_instance.login.assert_called_once_with("bot@example.com", "fake-app-password")
        assert smtp_instance.sendmail.called

        # Verify the raw message body contains the digest content
        raw_msg = smtp_instance.sendmail.call_args[0][2]
        assert email.subject in raw_msg
        assert "AAPL" in raw_msg


def test_digest_subject_contains_pct_change():
    """The subject line encodes the daily P&L percentage."""
    email = build_digest_email(_make_ctx())
    # Starting 104500, ending 103895 → roughly -0.58%
    assert "-0.58%" in email.subject


def test_digest_errors_section():
    """When errors are present they appear in html_body."""
    ctx = _make_ctx()
    ctx.errors.append("AlpacaRateLimit: 429 on /v2/orders")
    # Rebuild with the mutated context — re-create to keep immutability assumptions away
    ctx2 = DigestContext(
        date=ctx.date,
        starting_equity=ctx.starting_equity,
        ending_equity=ctx.ending_equity,
        realized_pnl=ctx.realized_pnl,
        unrealized_pnl=ctx.unrealized_pnl,
        regime=ctx.regime,
        active_config_version=ctx.active_config_version,
        trades=ctx.trades,
        errors=["AlpacaRateLimit: 429 on /v2/orders"],
    )
    email = build_digest_email(ctx2)
    assert "AlpacaRateLimit" in email.html_body
