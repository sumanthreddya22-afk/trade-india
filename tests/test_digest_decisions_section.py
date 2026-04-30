"""W1.5 — Daily digest surfaces decision counts + top rejection reasons.

Today rejections evaporate: only `placed_order` lands in trade_journal, so the
operator can see what the bot did but not what it considered. After W1.5,
the daily digest shows a "Decision Activity" block:

  Decision Activity (today)
  ─────────────────────────
  placed_order ............. 4
  rejected_by_risk ......... 2
  skipped_intel ............ 6
  hold ..................... 38

  Top rejection reasons
  ─────────────────────
  per_trade_risk: 1.20% > 1.00%   2
  earnings within 2 days          4
  ...
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from trading_bot.email_digest import DigestContext, build_daily_digest_email


def _ctx(**kwargs) -> DigestContext:
    base = dict(
        date=dt.date(2026, 4, 29),
        starting_equity=Decimal("100000"),
        ending_equity=Decimal("100250"),
        realized_pnl=Decimal("100"),
        unrealized_pnl=Decimal("150"),
        regime="trending_up",
        active_config_version="auto-20260429",
    )
    base.update(kwargs)
    return DigestContext(**base)


class TestNewFields:
    def test_decision_action_counts_default_empty(self):
        ctx = _ctx()
        assert ctx.decision_action_counts == {}

    def test_decision_top_rejection_reasons_default_empty(self):
        ctx = _ctx()
        assert ctx.decision_top_rejection_reasons == []


class TestRendering:
    def test_decision_activity_section_renders_when_present(self):
        ctx = _ctx(
            decision_action_counts={
                "placed_order": 4,
                "rejected_by_risk": 2,
                "skipped_intel": 6,
                "hold": 38,
            },
            decision_top_rejection_reasons=[
                ("per_trade_risk: 1.20% > 1.00%", 2),
                ("earnings within 2 days", 4),
            ],
        )
        email = build_daily_digest_email(ctx)
        html = email.html_body
        assert "Decision Activity" in html
        assert "placed_order" in html
        assert "rejected_by_risk" in html
        assert "earnings within 2 days" in html

    def test_decision_activity_omitted_when_no_decisions(self):
        ctx = _ctx()  # both fields empty
        email = build_daily_digest_email(ctx)
        # No bare "Decision Activity" header when counts are empty
        assert "Decision Activity" not in email.html_body

    def test_total_decisions_count_displayed(self):
        ctx = _ctx(
            decision_action_counts={"placed_order": 4, "hold": 96},
        )
        html = build_daily_digest_email(ctx).html_body
        # 100 total decisions today
        assert "100" in html
        assert "Decision Activity" in html
