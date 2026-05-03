"""Phase G — Event-driven ingestion tests.

Covers:
  * ingest_stream_event idempotency via UNIQUE(source, event_hash)
  * Stream-event mirror writes to intel_events (so Phase A-F see it)
  * unprocessed_events filters by source + processed_at IS NULL
  * mark_processed flips processed_at
  * dispatch_express splits scout vs hold by held_symbols set
  * DebateQueue: enqueue + top_n_unprocessed (priority order)
  * mark_outcome flips processed_at + outcome
  * expire_stale handles TTL
  * Sequential dispatch: top-N pop one at a time, no parallel
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot import debate_queue, event_streamer
from trading_bot.state_db import (
    Base, DebateQueue, IntelEvent, IntelStreamEvent, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Stream event ingestion
# ---------------------------------------------------------------------------


def test_ingest_stream_event_writes_row(engine):
    inserted = event_streamer.ingest_stream_event(
        engine, symbol="NVDA", asset_class="stock", source="sec_8k",
        headline="Q3 results", url="https://sec.gov/x",
        sentiment=0.7,
    )
    assert inserted is True
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelStreamEvent).all()
    assert len(rows) == 1
    assert rows[0].symbol == "NVDA"
    assert rows[0].source == "sec_8k"
    assert rows[0].sentiment == 0.7
    assert rows[0].processed_at is None


def test_ingest_stream_event_dedups_by_hash(engine):
    event_streamer.ingest_stream_event(
        engine, symbol="NVDA", asset_class="stock", source="sec_8k",
        headline="Q3", url="https://sec.gov/x",
    )
    inserted = event_streamer.ingest_stream_event(
        engine, symbol="NVDA", asset_class="stock", source="sec_8k",
        headline="Q3", url="https://sec.gov/x",
    )
    assert inserted is False  # duplicate
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        n = s.query(IntelStreamEvent).count()
    assert n == 1


def test_ingest_stream_event_mirrors_to_intel_events(engine):
    """Phase A-F consumers read from intel_events; the streamer mirrors
    into that table so existing rollup logic Just Works."""
    event_streamer.ingest_stream_event(
        engine, symbol="NVDA", asset_class="stock", source="sec_8k",
        headline="Q3", url="https://sec.gov/y",
        sentiment=0.7,
    )
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(IntelEvent.symbol == "NVDA").all()
    assert len(rows) == 1
    assert rows[0].source == "sec_8k"


# ---------------------------------------------------------------------------
# Unprocessed lookup + mark_processed
# ---------------------------------------------------------------------------


def test_unprocessed_events_filters_by_source(engine):
    event_streamer.ingest_stream_event(
        engine, symbol="A", asset_class="stock", source="sec_8k",
        url="https://x/1", headline="x",
    )
    event_streamer.ingest_stream_event(
        engine, symbol="B", asset_class="stock", source="vip_tweet",
        url="https://x/2", headline="x",
    )
    event_streamer.ingest_stream_event(
        engine, symbol="C", asset_class="stock", source="alpaca_news",
        url="https://x/3", headline="x",
    )
    rows = event_streamer.unprocessed_events(engine, sources=["sec_8k", "vip_tweet"])
    syms = {r.symbol for r in rows}
    assert syms == {"A", "B"}


def test_mark_processed_flips_processed_at(engine):
    event_streamer.ingest_stream_event(
        engine, symbol="A", asset_class="stock", source="sec_8k",
        url="https://x/1", headline="x",
    )
    rows = event_streamer.unprocessed_events(engine, sources=["sec_8k"])
    assert len(rows) == 1
    n = event_streamer.mark_processed(engine, ids=[rows[0].id])
    assert n == 1
    rows_again = event_streamer.unprocessed_events(engine, sources=["sec_8k"])
    assert rows_again == []


# ---------------------------------------------------------------------------
# Express dispatcher
# ---------------------------------------------------------------------------


def test_dispatch_express_splits_scout_vs_hold(engine):
    """A new symbol (not held) → scout. A held symbol → hold."""
    event_streamer.ingest_stream_event(
        engine, symbol="NEWSYM", asset_class="stock", source="sec_8k",
        url="https://x/1", headline="x",
    )
    event_streamer.ingest_stream_event(
        engine, symbol="HELD", asset_class="stock", source="sec_8k",
        url="https://x/2", headline="x",
    )
    out = event_streamer.dispatch_express(
        engine, sources=["sec_8k"], held_symbols={"HELD"},
    )
    assert out.n_processed == 2
    assert out.n_dispatched_scout == 1
    assert out.n_dispatched_hold == 1
    assert "HELD" in out.held_symbols


def test_dispatch_express_marks_all_processed(engine):
    event_streamer.ingest_stream_event(
        engine, symbol="A", asset_class="stock", source="sec_8k",
        url="https://x/1", headline="x",
    )
    event_streamer.dispatch_express(engine, sources=["sec_8k"])
    rows = event_streamer.unprocessed_events(engine, sources=["sec_8k"])
    assert rows == []


def test_dispatch_express_zero_when_no_events(engine):
    out = event_streamer.dispatch_express(engine, sources=["sec_8k"])
    assert out.n_processed == 0


def test_held_symbol_set_handles_provider_error():
    def bad_provider():
        raise ConnectionError("dns")
    assert event_streamer.held_symbol_set(bad_provider) == set()


def test_held_symbol_set_uppercases():
    out = event_streamer.held_symbol_set(lambda: [{"symbol": "nvda"}, {"symbol": "msft"}])
    assert out == {"NVDA", "MSFT"}


# ---------------------------------------------------------------------------
# DebateQueue
# ---------------------------------------------------------------------------


def test_enqueue_and_top_n_orders_by_priority(engine):
    now = dt.datetime.now(dt.timezone.utc)
    debate_queue.enqueue(engine, debate_class="entry", symbol="LOW",
                          asset_class="stock", priority_score=1.0, now=now)
    debate_queue.enqueue(engine, debate_class="entry", symbol="HIGH",
                          asset_class="stock", priority_score=10.0, now=now)
    debate_queue.enqueue(engine, debate_class="entry", symbol="MID",
                          asset_class="stock", priority_score=5.0, now=now)
    rows = debate_queue.top_n_unprocessed(engine, debate_class="entry", n=2)
    assert [r.symbol for r in rows] == ["HIGH", "MID"]


def test_top_n_excludes_processed(engine):
    now = dt.datetime.now(dt.timezone.utc)
    rid = debate_queue.enqueue(
        engine, debate_class="entry", symbol="A",
        asset_class="stock", priority_score=10.0, now=now,
    )
    debate_queue.mark_outcome(engine, row_id=rid, outcome="processed")
    rows = debate_queue.top_n_unprocessed(engine, debate_class="entry", n=10)
    assert rows == []


def test_top_n_filters_by_debate_class(engine):
    now = dt.datetime.now(dt.timezone.utc)
    debate_queue.enqueue(engine, debate_class="entry", symbol="E",
                          asset_class="stock", priority_score=5.0, now=now)
    debate_queue.enqueue(engine, debate_class="hold", symbol="H",
                          asset_class="stock", priority_score=5.0, now=now)
    entry_rows = debate_queue.top_n_unprocessed(engine, debate_class="entry", n=10)
    hold_rows = debate_queue.top_n_unprocessed(engine, debate_class="hold", n=10)
    assert {r.symbol for r in entry_rows} == {"E"}
    assert {r.symbol for r in hold_rows} == {"H"}


def test_expire_stale_marks_expired(engine):
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(hours=48)
    debate_queue.enqueue(engine, debate_class="entry", symbol="OLD",
                          asset_class="stock", priority_score=5.0, now=old)
    debate_queue.enqueue(engine, debate_class="entry", symbol="FRESH",
                          asset_class="stock", priority_score=5.0, now=now)
    n = debate_queue.expire_stale(engine, ttl_hours=24, now=now)
    assert n == 1
    rows = debate_queue.top_n_unprocessed(engine, debate_class="entry", n=10, now=now)
    assert {r.symbol for r in rows} == {"FRESH"}


def test_queue_depth_counts_only_unprocessed(engine):
    now = dt.datetime.now(dt.timezone.utc)
    rid = debate_queue.enqueue(
        engine, debate_class="entry", symbol="A",
        asset_class="stock", priority_score=5.0, now=now,
    )
    debate_queue.enqueue(engine, debate_class="entry", symbol="B",
                          asset_class="stock", priority_score=5.0, now=now)
    debate_queue.mark_outcome(engine, row_id=rid, outcome="processed")
    assert debate_queue.queue_depth(engine, debate_class="entry") == 1


def test_mark_outcome_demoted_keeps_row_visible_via_outcome(engine):
    """A demoted row is also "processed_at-flipped" but the outcome column
    audits why — useful for ops review."""
    now = dt.datetime.now(dt.timezone.utc)
    rid = debate_queue.enqueue(
        engine, debate_class="entry", symbol="A",
        asset_class="stock", priority_score=5.0, now=now,
    )
    debate_queue.mark_outcome(engine, row_id=rid, outcome="demoted")
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        row = s.get(DebateQueue, rid)
    assert row.outcome == "demoted"
    assert row.processed_at is not None
