"""Tests for the rebuilt daily digest email (B3)."""
import datetime as dt
from decimal import Decimal

import pytest


def _ctx(**overrides):
    from trading_bot.email_digest import DigestContext, TradeRow
    base = dict(
        date=dt.date(2026, 4, 28),
        starting_equity=Decimal("14984.16"),
        ending_equity=Decimal("14953.44"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("-12.74"),
        regime="trending_up",
        active_config_version="auto-20260428-100154",
        equity_30d=[Decimal("15000")] * 30,
        positions=[
            {"symbol": "BTCUSD", "qty": "0.000499", "side": "long",
             "entry": "76868.21", "current": "76334.10",
             "today_pct": "-0.66%", "total_pct": "-0.66%",
             "stop": "73020.00", "distance_pct": "4.34%",
             "sentiment": "—", "sector": "crypto"},
        ],
        version="phase4-v1",
        git_sha="faa4288",
    )
    base.update(overrides)
    return DigestContext(**base)


def test_digest_subject_uses_middle_dot():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx())
    assert " · " in email.subject
    assert "Daily Digest" in email.subject
    assert "Apr 28" in email.subject


def test_digest_body_contains_all_section_headers():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx())
    for label in ["EQUITY", "RISK", "REGIME", "POSITIONS"]:
        assert label.upper() in email.html_body.upper()


def test_digest_renders_kpi_grid_with_equity():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx())
    assert "$14,953.44" in email.html_body or "14,953" in email.html_body


def test_digest_renders_position_rows():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx())
    assert "BTCUSD" in email.html_body
    assert "long" in email.html_body.lower()


def test_digest_status_amber_when_audit_warnings_present():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx(
        schedule_audit_warnings=[
            {"job_id": "stock_scanner", "expected": 7, "actual": 0, "ratio": 0.0},
        ],
    ))
    assert "stock_scanner" in email.html_body
    # Pulse-dot is amber for warn
    assert "#fbbf24" in email.html_body


def test_digest_includes_wheel_section():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx(
        wheel_open_cycles=[
            {"symbol": "AAPL", "phase": "csp_open", "strike": "190",
             "expiration": "2026-05-30", "dte": 32, "delta": -0.27,
             "iv": "0.30", "credit": "2.10", "mark": "1.20",
             "pnl": "+90", "trigger_distance": "8 days to 21-DTE"},
        ],
        wheel_pnl_mtd=Decimal("325"),
        wheel_collateral_pct=8.5,
        wheel_win_rate=0.80,
    ))
    assert "Wheel" in email.html_body or "WHEEL" in email.html_body
    assert "AAPL" in email.html_body
    assert "csp_open" in email.html_body
    assert "$325" in email.html_body


def test_digest_renders_lab_promotion_section_when_pending():
    from trading_bot.email_digest import build_daily_digest_email
    email = build_daily_digest_email(_ctx(
        pending_promotions=[{
            "version": "auto-20260428-100154",
            "fitness_at_promotion": 3.967,
            "scans_since_promote": 12,
            "entries_since_promote": 0,
            "near_misses_since_promote": 5,
            "params": {"rsi_lower": 50.07, "stop_pct": 6.11},
            "risk_caps": {},
        }],
    ))
    assert "auto-20260428-100154" in email.html_body
    assert "3.97" in email.html_body or "3.967" in email.html_body
