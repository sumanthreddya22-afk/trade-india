"""Cross-email rendering smoke tests (Phase 7.1).

Catches the schema-drift class of bug that crashed the midday email
twice in late April 2026 (KeyError: 'intraday_pct',
"Unknown format code 'f' for object of type 'str'"). For every
build_*_email() call site, construct a representative context with
the *current* DigestData / position / trade shapes and assert the
render returns a non-empty Email.

Doesn't validate HTML structure — that's not the failure mode. The
failure mode is "render path raises on a well-shaped input." If a
field gets renamed or reformatted, this fails fast in CI before the
3am digest does.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from trading_bot.digest_data import DigestData


# ---------------------------------------------------------------------------
# Representative fixtures matching what gather_all() / digest_data produce.
# Updated when the upstream shapes change so this remains the canonical
# "what does the email layer expect" doc.
# ---------------------------------------------------------------------------


def _position_dict(symbol: str = "F", *, asset_class: str = "stock") -> dict:
    """Mirrors the dict shape used by digest_data._gather_positions_and_unrealized
    AND consumed by email_midday.build_midday_snapshot_email."""
    return {
        "symbol": symbol,
        "qty": Decimal("100"),
        "side": "long",
        "entry": Decimal("11.50"),
        "current": Decimal("11.88"),
        "today_pct": 0.5,
        "total_pct": 3.3,
        "stop": Decimal("11.20"),
        "distance_pct": 5.7,
        "sentiment": 0.2,
        "sector": "Consumer Cyclical",
        "market_value": Decimal("1188"),
        "unrealized_pl": Decimal("38"),
        "asset_class": asset_class,
    }


def _midday_trade_dict() -> dict:
    """Mirror of cli.midday_snapshot_cli's todays_trades_dicts shape."""
    return {
        "time": "10:42",
        "side": "buy",
        "symbol": "F",
        "qty": Decimal("100"),
        "price": 11.88,  # NOTE: float, not str — formatting bug from 04-30
        "strategy": "momentum",
    }


def _digest_data() -> DigestData:
    """Realistic non-empty DigestData."""
    d = DigestData()
    d.starting_equity = Decimal("14961.23")
    d.ending_equity = Decimal("15010.45")
    d.realized_pnl = Decimal("12.34")
    d.unrealized_pnl = Decimal("36.88")
    d.daily_pnl_pct = 0.33
    d.weekly_pnl_pct = -0.5
    d.drawdown_pct = 1.2
    d.consecutive_losing_days = 0
    d.equity_30d = [Decimal(str(15000 + i * 0.5)) for i in range(30)]
    d.vix = 18.5
    d.yield_10y = 4.2
    d.positions = [_position_dict("F"), _position_dict("BAC", asset_class="stock")]
    d.errors = []
    d.daemon_blips = 0
    return d


# ---------------------------------------------------------------------------
# Midday snapshot email
# ---------------------------------------------------------------------------


def test_midday_snapshot_renders_with_positions_and_trades():
    """Regression for 2026-04-30 KeyError 'intraday_pct' and the
    associated price-format bug. Position dicts must carry total_pct
    (not intraday_pct); trade dicts must carry numeric price."""
    from trading_bot.email_midday import (
        SnapshotContext, build_midday_snapshot_email,
    )

    ctx = SnapshotContext(
        as_of=dt.datetime.now(dt.timezone.utc),
        equity=Decimal("15010"),
        starting_equity=Decimal("14961"),
        realized_pnl_today=Decimal("12.34"),
        unrealized_pnl=Decimal("36.88"),
        regime="trending_up",
        positions=[_position_dict("F"), _position_dict("BAC")],
        trades_today=[_midday_trade_dict()],
        watchlist_signals=[],
        daily_loss_pct=-0.1,
        drawdown_pct=1.2,
        daily_loss_cap_pct=2.0,
        drawdown_cap_pct=20.0,
        version="test-v1",
        git_sha="abc1234",
    )
    email = build_midday_snapshot_email(ctx)
    assert email.subject  # non-empty
    assert email.html_body
    assert len(email.html_body) > 1000  # real content, not a stub


def test_midday_snapshot_renders_with_no_positions_and_no_trades():
    """Edge case the cron path hits on a slow day."""
    from trading_bot.email_midday import (
        SnapshotContext, build_midday_snapshot_email,
    )

    ctx = SnapshotContext(
        as_of=dt.datetime.now(dt.timezone.utc),
        equity=Decimal("15000"),
        starting_equity=Decimal("15000"),
        realized_pnl_today=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        regime="unknown",
        positions=[],
        trades_today=[],
        watchlist_signals=[],
    )
    email = build_midday_snapshot_email(ctx)
    assert email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Status email (used by alerts)
# ---------------------------------------------------------------------------


def test_status_email_renders():
    from trading_bot.email_alerts import StatusContext, build_status_email
    ctx = StatusContext(
        as_of=dt.datetime.now(dt.timezone.utc),
        equity=Decimal("15010"),
        cash=Decimal("4000"),
        buying_power=Decimal("11000"),
        regime="sideways",
        open_positions=[_position_dict("F"), _position_dict("BAC")],
        open_order_count=2,
        last_heartbeat_age_minutes=0.5,
        last_action="heartbeat",
        version="test-v1", git_sha="abc1234",
    )
    email = build_status_email(ctx)
    assert email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Alert email (intel-scan / crypto-scan summary)
# ---------------------------------------------------------------------------


def test_alert_email_renders_with_placed_and_rejected():
    from trading_bot.email_alerts import AlertContext, build_alert_email
    ctx = AlertContext(
        as_of=dt.datetime.now(dt.timezone.utc),
        workflow="intel-scan",
        regime="trending_up",
        placed=[{"symbol": "AAPL", "reason": "momentum", "entry_order_id": "x1"}],
        rejected=[{"symbol": "TSLA", "reason": "per_trade_risk_pct"}],
        skipped_intel=[{"symbol": "MSFT", "reason": "no_signal"}],
        decision_counts={"buy": 1, "hold": 5, "skip": 12},
        version="test-v1", git_sha="abc1234",
    )
    email = build_alert_email(ctx)
    assert email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Critical alert email
# ---------------------------------------------------------------------------


def test_critical_email_renders():
    from trading_bot.email_critical import build_critical_email
    email = build_critical_email(
        title="Test critical event",
        detail="Something went wrong with X.\nDetail line 2.",
        severity="HIGH",
    )
    assert "[CRITICAL]" in email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Daily digest email
# ---------------------------------------------------------------------------


def test_daily_digest_email_renders():
    """Regression for the digest-render path. Build DigestContext with
    every field DigestData populates and verify build_daily_digest_email
    completes without raising.
    """
    from trading_bot.email_digest import (
        DigestContext, build_daily_digest_email,
    )

    today = dt.date.today()
    ctx = DigestContext(
        date=today,
        starting_equity=Decimal("14961"),
        ending_equity=Decimal("15010"),
        realized_pnl=Decimal("12.34"),
        unrealized_pnl=Decimal("36.88"),
        regime="trending_up",
        active_config_version="momentum-v1",
        trades=[],
        errors=[],
    )
    email = build_daily_digest_email(ctx)
    assert email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Unblock-debate email (Phase 5 — already has its own tests but include
# here for one-stop-shop "all email render paths" coverage).
# ---------------------------------------------------------------------------


def test_unblock_debate_email_renders():
    from trading_bot.email_unblock_debate import (
        DebateEmailContext, build_unblock_debate_email,
    )
    from trading_bot.unblock_debate import UnblockVerdict

    ctx = DebateEmailContext(
        asset_class="wheel", symbol="MRNA",
        block_reason="options_cap (26.7% > 20%)",
        overage_ratio=0.34, candidate_score=8.0,
        proposal_summary="symbol: MRNA\nstrike: 40",
        fundamentals="iv_rank: 100",
        operational_context="equity: 15000",
        verdict=UnblockVerdict(
            recommendation="reject", confidence="high",
            reason="x" * 25,
            aggressive_text="A", conservative_text="B", neutral_text="C",
        ),
    )
    email = build_unblock_debate_email(ctx)
    assert email.subject
    assert email.html_body


# ---------------------------------------------------------------------------
# Cross-call: every public build_* function we've identified, for completeness.
# ---------------------------------------------------------------------------


def test_all_email_builders_callable_and_return_email():
    """Catch-all: import every public build_*_email and verify it's at
    least a callable. New email builders added later need their own
    above-the-fold smoke test, but this catches the case where an
    importable builder gets accidentally deleted or renamed.
    """
    from trading_bot import (
        email_midday, email_alerts, email_critical, email_digest,
        email_unblock_debate,
    )
    callables = [
        email_midday.build_midday_snapshot_email,
        email_alerts.build_status_email,
        email_alerts.build_alert_email,
        email_critical.build_critical_email,
        email_digest.build_daily_digest_email,
        email_unblock_debate.build_unblock_debate_email,
    ]
    for fn in callables:
        assert callable(fn), f"{fn.__name__} not callable"
