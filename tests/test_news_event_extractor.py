"""W4.1 — News-event extractor tests.

Deterministic NLP layer that aggregates a cluster of NewsItems into a
StructuredEvent: entity, event_type, direction, novelty, source_count,
primary_filing_present, market_reaction. Output feeds the news_controller
LLM prompt.
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading_bot.intelligence import NewsItem
from trading_bot.news_trader.event_extractor import (
    StructuredEvent,
    extract_event,
    detect_event_type,
    detect_direction,
)


def _item(headline: str, *, source: str = "alpaca", symbols=("XYZ",), summary: str = "",
          published_at: dt.datetime | None = None) -> NewsItem:
    return NewsItem(
        headline=headline, summary=summary, url=f"https://example.com/{abs(hash(headline)) % 1_000_000}",
        published_at=published_at or dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.timezone.utc),
        symbols=list(symbols), source=source,
    )


class TestEventTypeDetection:
    def test_earnings_event(self):
        assert detect_event_type("XYZ Q1 Earnings Beat Wall Street Estimates") == "earnings"
        assert detect_event_type("Q3 Earnings: Revenue up 12%") == "earnings"

    def test_merger_event(self):
        assert detect_event_type("XYZ to Acquire ABC Inc for $5B") == "merger_acquisition"
        assert detect_event_type("Merger Agreement Announced") == "merger_acquisition"

    def test_strategic_alternatives_rumor(self):
        assert detect_event_type("XYZ Corp exploring strategic alternatives") == "strategic_alternatives"

    def test_guidance_event(self):
        assert detect_event_type("XYZ Raises Full-Year Guidance") == "guidance"
        assert detect_event_type("Cuts Q4 Guidance Citing Weak Demand") == "guidance"

    def test_lawsuit_event(self):
        assert detect_event_type("Class Action Lawsuit Filed Against XYZ") == "litigation"

    def test_unknown_falls_to_general(self):
        assert detect_event_type("XYZ Names New CEO") == "leadership_change"
        assert detect_event_type("Some Random Unparseable Headline") == "general"


class TestDirection:
    def test_positive_words(self):
        assert detect_direction("XYZ beats earnings, raises guidance") == "POSITIVE"
        assert detect_direction("Strong revenue surge in Q1") == "POSITIVE"

    def test_negative_words(self):
        assert detect_direction("XYZ misses earnings, cuts guidance") == "NEGATIVE"
        assert detect_direction("Lawsuit filed, stock plunges") == "NEGATIVE"

    def test_mixed_returns_mixed(self):
        assert detect_direction("XYZ exploring strategic alternatives") == "MIXED"


class TestExtractEvent:
    def test_single_item_returns_event(self):
        items = [_item("XYZ Reports Strong Q1 Earnings", symbols=("XYZ",))]
        ev = extract_event(items, entity="XYZ")
        assert ev is not None
        assert ev.entity == "XYZ"
        assert ev.event_type == "earnings"
        assert ev.direction == "POSITIVE"
        assert ev.source_count == 1
        assert 0.0 <= ev.novelty <= 1.0

    def test_multiple_sources_increment_count(self):
        items = [
            _item("XYZ to Acquire ABC", source="alpaca"),
            _item("XYZ Corp Acquires ABC for $5B", source="polygon"),
            _item("Acquisition Deal Announced", source="gdelt"),
        ]
        ev = extract_event(items, entity="XYZ")
        assert ev is not None
        assert ev.source_count == 3
        assert ev.event_type == "merger_acquisition"

    def test_dedupes_same_source(self):
        items = [
            _item("XYZ Reports Q1", source="alpaca"),
            _item("XYZ Reports Q1", source="alpaca"),  # exact dup
        ]
        ev = extract_event(items, entity="XYZ")
        assert ev.source_count == 1  # de-duped

    def test_primary_filing_detected(self):
        items = [_item("XYZ Files 8-K with SEC", source="sec_edgar")]
        ev = extract_event(items, entity="XYZ")
        assert ev.primary_filing_present is True

    def test_rumor_only_no_primary_filing(self):
        items = [_item("Rumor: XYZ exploring sale", source="reddit")]
        ev = extract_event(items, entity="XYZ")
        assert ev.primary_filing_present is False
        assert ev.event_type == "strategic_alternatives"

    def test_empty_returns_none(self):
        ev = extract_event([], entity="XYZ")
        assert ev is None
