"""Tests for the intel candidate pool — scoring, aggregation, lane wiring.

The pool replaces hardcoded universe lists with a continuous internet-driven
candidate set. Properties tested here are the contract between the
ingestor and downstream consumers (daemon, dashboard):

  * Scoring is deterministic and decays with age.
  * Higher-trust sources (SEC > news > social) outweigh lower-trust ones.
  * Cross-source confirmation gives a meaningful bonus.
  * The aggregator's UPSERT path is idempotent across re-runs.
  * Lane wiring falls back transparently when the pool is empty/stale.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.intel import aggregator, pool
from trading_bot.state_db import Base, IntelCandidate, IntelEvent, get_engine


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Pure scoring functions (no DB)
# ---------------------------------------------------------------------------


def test_event_score_decays_with_age():
    fresh = aggregator.event_score(source="alpaca_news", sentiment=0.0, age_hours=0.5)
    stale = aggregator.event_score(source="alpaca_news", sentiment=0.0, age_hours=24.0)
    assert fresh > stale * 2  # 24h is multiple decay periods on alpaca_news


def test_event_score_high_trust_source_outweighs_low_trust():
    # Same age, no sentiment — SEC 8-K should beat social.
    sec = aggregator.event_score(source="sec_8k", sentiment=0.0, age_hours=1.0)
    social = aggregator.event_score(source="apewisdom", sentiment=0.0, age_hours=1.0)
    assert sec > social * 2


def test_event_score_sentiment_amplifies_modestly():
    neutral = aggregator.event_score(source="alpaca_news", sentiment=0.0, age_hours=1.0)
    bullish = aggregator.event_score(source="alpaca_news", sentiment=1.0, age_hours=1.0)
    bearish = aggregator.event_score(source="alpaca_news", sentiment=-1.0, age_hours=1.0)
    # Strong opinion (either direction) raises score by 50%.
    assert bullish == pytest.approx(neutral * 1.5)
    assert bearish == pytest.approx(neutral * 1.5)


def test_event_score_unknown_source_uses_default_weight():
    """Forward-compat: a source we haven't tuned shouldn't crash."""
    score = aggregator.event_score(source="brand_new_feed", sentiment=0.0, age_hours=1.0)
    assert score > 0


def test_symbol_score_cross_source_bonus_is_monotonic():
    """More distinct sources → higher total. Single-source spam can't
    outscore broad coverage even with massive sum."""
    one_source = aggregator.symbol_score(sum_event_score=10.0, n_distinct_sources=1)
    three_sources = aggregator.symbol_score(sum_event_score=10.0, n_distinct_sources=3)
    six_sources = aggregator.symbol_score(sum_event_score=10.0, n_distinct_sources=6)
    assert one_source < three_sources < six_sources


# ---------------------------------------------------------------------------
# write_event + roll_up integration
# ---------------------------------------------------------------------------


def test_write_event_dedups_on_hash(engine):
    """Re-inserting the same (symbol, source, hash) is a no-op."""
    ok1 = aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="alpaca_news", headline="iPhone 17 demand strong",
        url="https://example.com/aapl-1",
    )
    ok2 = aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="alpaca_news", headline="iPhone 17 demand strong",
        url="https://example.com/aapl-1",
    )
    assert ok1 is True
    assert ok2 is False  # dedup


def test_write_event_distinct_urls_both_recorded(engine):
    aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="alpaca_news", url="https://example.com/aapl-1",
    )
    aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="alpaca_news", url="https://example.com/aapl-2",
    )
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        n = s.query(IntelEvent).count()
    assert n == 2


def test_roll_up_aggregates_to_one_row_per_symbol(engine):
    """Three events for the same symbol, three different sources → 1
    candidate row with cross-source bonus applied."""
    aggregator.write_event(
        engine, symbol="NVDA", asset_class="stock",
        source="alpaca_news", url="u1", sentiment=0.5,
    )
    aggregator.write_event(
        engine, symbol="NVDA", asset_class="stock",
        source="sec_8k", url="u2", sentiment=0.0,
    )
    aggregator.write_event(
        engine, symbol="NVDA", asset_class="stock",
        source="apewisdom", url="u3", sentiment=0.3,
    )
    aggregator.roll_up(engine)
    entries = pool.top_for_asset_class(engine, "stock", n=10, min_score=0.0)
    assert len(entries) == 1
    nvda = entries[0]
    assert nvda.symbol == "NVDA"
    assert nvda.n_mentions == 3
    assert nvda.n_sources == 3
    assert nvda.score > 0
    assert set(nvda.sources.keys()) == {"alpaca_news", "sec_8k", "apewisdom"}


def test_roll_up_is_idempotent(engine):
    """Re-rolling on the same events doesn't create duplicate candidate rows."""
    aggregator.write_event(
        engine, symbol="MSFT", asset_class="stock",
        source="alpaca_news", url="u1",
    )
    aggregator.roll_up(engine)
    aggregator.roll_up(engine)
    aggregator.roll_up(engine)
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        n = s.query(IntelCandidate).filter_by(symbol="MSFT").count()
    assert n == 1  # UPSERT, not INSERT


def test_roll_up_higher_trust_source_outranks_lower(engine):
    """SEC-only candidate beats apewisdom-only candidate of same recency."""
    aggregator.write_event(
        engine, symbol="HIGH", asset_class="stock",
        source="sec_8k", url="sec",
    )
    aggregator.write_event(
        engine, symbol="LOW", asset_class="stock",
        source="apewisdom", url="ape",
    )
    aggregator.roll_up(engine)
    entries = pool.top_for_asset_class(engine, "stock", n=10, min_score=0.0)
    syms = [e.symbol for e in entries]
    assert syms == ["HIGH", "LOW"]
    high = next(e for e in entries if e.symbol == "HIGH")
    low = next(e for e in entries if e.symbol == "LOW")
    assert high.score > low.score


def test_top_for_asset_class_respects_min_score(engine):
    """The min_score parameter filters candidates below the requested floor.
    A 1-mention apewisdom row scores ~3.4; raising the floor above that
    excludes it while min_score=0 includes it."""
    aggregator.write_event(
        engine, symbol="NOISE", asset_class="stock",
        source="apewisdom", url="x",
    )
    aggregator.roll_up(engine)
    visible = pool.top_for_asset_class(engine, "stock", n=10, min_score=0.0)
    assert len(visible) == 1
    filtered = pool.top_for_asset_class(engine, "stock", n=10, min_score=10.0)
    assert len(filtered) == 0  # above the candidate's score


def test_top_for_asset_class_respects_max_age(engine):
    """Candidate older than max_age_hours doesn't surface."""
    now = dt.datetime.now(dt.timezone.utc)
    old_event_at = now - dt.timedelta(hours=72)
    aggregator.write_event(
        engine, symbol="STALE", asset_class="stock",
        source="sec_8k", url="x", event_at=old_event_at,
        now=now - dt.timedelta(hours=72),
    )
    aggregator.roll_up(engine, now=now)
    fresh = pool.top_for_asset_class(engine, "stock", max_age_hours=24, min_score=0.0)
    assert fresh == []


def test_is_pool_fresh_false_when_empty(engine):
    assert pool.is_pool_fresh(engine) is False


def test_is_pool_fresh_true_after_recent_roll_up(engine):
    aggregator.write_event(
        engine, symbol="X", asset_class="stock", source="sec_8k", url="u",
    )
    aggregator.roll_up(engine)
    assert pool.is_pool_fresh(engine) is True


def test_is_pool_fresh_false_after_window(engine):
    """Manually backdate rolled_up_at to simulate stale ingest."""
    aggregator.write_event(
        engine, symbol="X", asset_class="stock", source="sec_8k", url="u",
    )
    aggregator.roll_up(engine)
    # Manually backdate.
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        c = s.query(IntelCandidate).first()
        c.rolled_up_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        s.commit()
    assert pool.is_pool_fresh(engine, max_age_hours=2) is False


def test_pool_separates_by_asset_class(engine):
    aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="sec_8k", url="aapl",
    )
    aggregator.write_event(
        engine, symbol="BTC/USD", asset_class="crypto",
        source="apewisdom", url="btc",
    )
    aggregator.roll_up(engine)
    stocks = pool.top_for_asset_class(engine, "stock", min_score=0.0)
    cryptos = pool.top_for_asset_class(engine, "crypto", min_score=0.0)
    assert {e.symbol for e in stocks} == {"AAPL"}
    assert {e.symbol for e in cryptos} == {"BTC/USD"}


def test_lookup_returns_specific_pool_entry(engine):
    aggregator.write_event(
        engine, symbol="NVDA", asset_class="stock", source="alpaca_news", url="u",
    )
    aggregator.roll_up(engine)
    e = pool.lookup(engine, "NVDA", "stock")
    assert e is not None
    assert e.symbol == "NVDA"
    assert e.n_mentions == 1


def test_lookup_returns_none_for_missing(engine):
    assert pool.lookup(engine, "GHOST", "stock") is None


# ---------------------------------------------------------------------------
# Daemon-side wiring (cold-start fallback semantics)
# ---------------------------------------------------------------------------


def test_load_intel_pool_stocks_returns_empty_when_pool_stale(monkeypatch, engine):
    """Lane wiring: when the pool is stale, _load_intel_pool_stocks returns
    [], the caller transparently falls through to opportunities.md / seed."""
    from trading_bot import cli as cli_mod
    monkeypatch.setattr(cli_mod, "STATE_DB_PATH", str(engine.url.database))
    # Pool is empty by definition (fresh DB) — should return [].
    result = cli_mod._load_intel_pool_stocks()
    assert result == []


def test_load_intel_pool_stocks_surfaces_pool_when_fresh(monkeypatch, engine):
    """When pool has fresh rows, we get WatchlistEntry list ordered by score."""
    from trading_bot import cli as cli_mod
    monkeypatch.setattr(cli_mod, "STATE_DB_PATH", str(engine.url.database))
    aggregator.write_event(
        engine, symbol="NVDA", asset_class="stock",
        source="sec_8k", url="u1",
    )
    aggregator.write_event(
        engine, symbol="AAPL", asset_class="stock",
        source="alpaca_news", url="u2",
    )
    aggregator.roll_up(engine)
    out = cli_mod._load_intel_pool_stocks()
    assert len(out) >= 1
    for e in out:
        assert e.asset_class == "us_equity"  # WatchlistEntry conventions
        assert e.notes.startswith("intel:")
