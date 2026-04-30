"""Midday snapshot only renders the freshness section when caches are stale.

Silent-green is the steady state. Operator only hears about staleness when
something is actually over budget.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from trading_bot.email_midday import SnapshotContext, build_midday_snapshot_email
from trading_bot.freshness_audit import FreshnessFinding


def _ctx() -> SnapshotContext:
    return SnapshotContext(
        as_of=dt.datetime(2026, 4, 30, 16, 0, tzinfo=dt.timezone.utc),
        equity=Decimal("100000"),
        starting_equity=Decimal("100000"),
        realized_pnl_today=Decimal("0"),
        unrealized_pnl=Decimal("50"),
        regime="trending_up",
    )


class TestMiddayFreshnessSection:
    def test_silent_when_all_caches_fresh(self):
        all_ok = [
            FreshnessFinding(
                cache="news_sentiment", last_seen="x", age_hours=10.0,
                budget_hours=24.0, severity="ok", note="n",
            ),
        ]
        with patch("trading_bot.freshness_audit.audit_freshness", return_value=all_ok):
            html = build_midday_snapshot_email(_ctx()).html_body
        assert "Stale Data" not in html

    def test_renders_when_a_cache_is_stale(self):
        findings = [
            FreshnessFinding(
                cache="news_sentiment", last_seen="2026-04-29 00:00",
                age_hours=40.0, budget_hours=24.0, severity="stale",
                note="news_warm should run twice daily",
            ),
        ]
        with patch("trading_bot.freshness_audit.audit_freshness", return_value=findings):
            html = build_midday_snapshot_email(_ctx()).html_body
        assert "Stale Data" in html
        assert "news_sentiment" in html
        assert "40.0h" in html

    def test_audit_failure_does_not_break_snapshot(self):
        with patch("trading_bot.freshness_audit.audit_freshness",
                   side_effect=RuntimeError("boom")):
            html = build_midday_snapshot_email(_ctx()).html_body
        assert "Midday Snapshot" in html
        assert "Stale Data" not in html
