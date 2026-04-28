# src/trading_bot/roles/sentiment_analyst.py
"""Sentiment Analyst — Tier 1. Refreshes per-symbol Polygon news+sentiment
cache (3-day TTL). Two scheduled warms: 08:55 ET pre-open, 12:00 ET midday."""
from __future__ import annotations
from trading_bot.roles.runner import BaseRole


class SentimentAnalystRole(BaseRole):
    name = "sentiment_analyst"
    tier = 1
    process = "daemon"
    job_description = (
        "Refresh per-symbol news+sentiment for stage-2 watchlist via "
        "Polygon news API. Two scheduled warms (08:55 ET, 12:00 ET) plus "
        "on-demand inline at scan time when a candidate is > 4h stale."
    )
    sla_seconds = 60
    upstream_roles = ["universe_curator"]
    downstream_roles = ["stock_scanner"]

    def _do_work(self, ctx):
        from trading_bot import cli as cli_mod
        cli_mod.news_warm.callback(lookback_days=3)
        return {"completed": True}

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        # Floor-block accuracy (% of names blocked by sentiment floor whose
        # next-5d return was negative). Activates in Phase 3 with journal data.
        return (
            "floor_block_post_5d_return",
            0.0,
            "KPI activates in Phase 3 (requires journal of sentiment-blocked names)",
        )
