"""Intel pool — continuous internet-driven candidate aggregation.

The package replaces the bot's static-list universe construction with a
score-decayed pool of symbols sourced from news, filings, social, and
macro feeds. Hot path consumes from ``pool.top_for_asset_class``; the
ingestor role populates the pool every 30-60 min.

Public API:
  * ``pool.top_for_asset_class(engine, asset_class, n=...)``
        Read top-N candidates by score. Daemon calls this at scan time.
  * ``pool.lookup(engine, symbol, asset_class)``
        One-row read for dashboard / debug.
  * ``pool.is_pool_fresh(engine, max_age_hours=2)``
        Universe sources fall back to existing screeners when this is False.
  * ``aggregator.roll_up(engine)``
        Materialize ``intel_candidates`` from raw events. Called by the
        ingestor after writing new events.
  * ``sources.collect_all(engine, settings, ...)``
        Pull from every wired source, write events. Idempotent via
        ``event_hash`` dedup.
"""
from trading_bot.intel import aggregator, pool, sources  # noqa: F401

__all__ = ["aggregator", "pool", "sources"]
