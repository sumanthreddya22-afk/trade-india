"""Tests for the dedicated status + alert email templates.

Replaces the prior pattern where 92% of operational emails rendered the full
12-section daily-digest skeleton with empty data, making every email look the
same.  These tests pin the new visual identity:

  status email — terse account snapshot (no daily-digest sections)
  alert email — focused on placed / rejected / skipped (no KPI grid)
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from trading_bot.email_alerts import (
    AlertContext, StatusContext,
    build_alert_email, build_status_email,
)


def _now():
    return dt.datetime(2026, 4, 30, 16, 0, tzinfo=dt.timezone.utc)


class TestStatusEmail:
    def test_subject_includes_equity_and_position_count(self):
        ctx = StatusContext(
            as_of=_now(), equity=Decimal("14934.99"),
            cash=Decimal("14386.77"), buying_power=Decimal("28771.54"),
            regime="trending_up",
            open_positions=[
                {"symbol": "BTCUSD", "qty": "0.0005",
                 "avg_entry_price": 76868.21, "market_value": 38.05,
                 "unrealized_pl": 0.03},
            ],
            open_order_count=2, last_heartbeat_age_minutes=0.8,
            last_action="alert_drain",
        )
        email = build_status_email(ctx)
        # Subject must encode the things that change between consecutive
        # status emails — without this the inbox looks like 39 dupes.
        assert "$14,935" in email.subject or "$14,934" in email.subject
        assert "1 positions" in email.subject
        assert "trending_up" in email.subject

    def test_renders_open_positions(self):
        ctx = StatusContext(
            as_of=_now(), equity=Decimal("15000"), cash=Decimal("15000"),
            buying_power=Decimal("30000"), regime="trending_up",
            open_positions=[
                {"symbol": "NVDA", "qty": "10",
                 "avg_entry_price": 500.0, "market_value": 5100.0,
                 "unrealized_pl": 100.0},
            ],
        )
        html = build_status_email(ctx).html_body
        assert "NVDA" in html
        assert "Open Positions" in html

    def test_no_positions_renders_empty_message(self):
        ctx = StatusContext(
            as_of=_now(), equity=Decimal("15000"), cash=Decimal("15000"),
            buying_power=Decimal("30000"), regime="risk_off",
        )
        html = build_status_email(ctx).html_body
        assert "No open positions" in html

    def test_status_does_not_render_daily_digest_sections(self):
        """The whole point — the status email should NOT include the
        daily-digest's 'Risk gauges' / 'Closed trades 7d' / 'Watchlist
        movers' / 'Pending promotions' / etc. sections."""
        ctx = StatusContext(
            as_of=_now(), equity=Decimal("15000"), cash=Decimal("15000"),
            buying_power=Decimal("30000"), regime="trending_up",
        )
        html = build_status_email(ctx).html_body
        forbidden = [
            "Closed Trades", "Closed trades", "Watchlist Movers",
            "Pending Promotions", "Sentiment", "EOD Session",
            "Daily Digest",
        ]
        for f in forbidden:
            assert f not in html, f"status email leaked digest section: {f!r}"


class TestAlertEmail:
    def test_subject_encodes_action(self):
        ctx = AlertContext(
            as_of=_now(), workflow="intel-scan", regime="trending_up",
            placed=[{"symbol": "ARM", "reason": "rsi=64",
                     "entry_order_id": "abc1234567890"}],
            rejected=[{"symbol": "SHIB/USD",
                       "reason": "per_trade_risk_pct: 4.5% > 1%"}],
        )
        email = build_alert_email(ctx)
        assert "1 placed" in email.subject
        assert "1 rejected" in email.subject
        assert "trending_up" in email.subject
        assert "Intel-Scan" in email.subject

    def test_placed_section_renders(self):
        ctx = AlertContext(
            as_of=_now(), workflow="intel-scan", regime="trending_up",
            placed=[{"symbol": "ARM", "reason": "rsi=64", "entry_order_id": "x"}],
        )
        html = build_alert_email(ctx).html_body
        assert "Placed (1)" in html
        assert "ARM" in html

    def test_rejected_section_renders(self):
        ctx = AlertContext(
            as_of=_now(), workflow="crypto-scan", regime="trending_up",
            rejected=[{"symbol": "SHIB/USD", "reason": "risk too high"}],
        )
        html = build_alert_email(ctx).html_body
        assert "Rejected by Risk" in html
        assert "SHIB/USD" in html
        assert "risk too high" in html

    def test_alert_does_not_render_daily_digest_sections(self):
        ctx = AlertContext(
            as_of=_now(), workflow="intel-scan", regime="trending_up",
            placed=[{"symbol": "ARM", "reason": "x", "entry_order_id": "y"}],
        )
        html = build_alert_email(ctx).html_body
        forbidden = [
            "Closed Trades (last 7d)", "Watchlist Movers",
            "EOD Session Review", "Equity 30d",
        ]
        for f in forbidden:
            assert f not in html, f"alert email leaked digest section: {f!r}"

    def test_decision_counts_table_renders(self):
        ctx = AlertContext(
            as_of=_now(), workflow="intel-scan", regime="trending_up",
            decision_counts={"hold": 38, "rejected_by_risk": 1, "placed_order": 1},
        )
        html = build_alert_email(ctx).html_body
        assert "Decision Activity" in html
        assert "hold" in html
        assert "38" in html

    def test_subject_distinguishes_workflows(self):
        intel = build_alert_email(AlertContext(
            as_of=_now(), workflow="intel-scan", regime="r",
        )).subject
        crypto = build_alert_email(AlertContext(
            as_of=_now(), workflow="crypto-scan", regime="r",
        )).subject
        assert intel != crypto
        assert "Intel-Scan" in intel
        assert "Crypto-Scan" in crypto
