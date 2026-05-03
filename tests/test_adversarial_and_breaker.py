"""Phase F — Adversarial defense + circuit breaker tests.

Adversarial:
  * URL hash dedup normalises query strings + scheme
  * detect_suspicious_spike fires on cold-start spikes and >10x median
  * detect_coordinated fires on 3+ near-identical headlines from cold base
  * detect_pump_signature fires on heavy social + neutral news + small-cap

Circuit breaker:
  * trip + clear + state machine
  * Auto-clear on expires_at
  * evaluate_metrics priority order (VIX > drawdown > losses > stops > api)

Aggregator integration:
  * roll_up persists adversarial flags onto IntelCandidate
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from trading_bot import circuit_breaker
from trading_bot.intel import adversarial
from trading_bot.intel.aggregator import roll_up
from trading_bot.state_db import (
    Base, CircuitBreakerEvent, IntelCandidate, IntelEvent, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


def _add_event(engine, *, symbol="A", source="alpaca_news",
               headline="x", url="https://x", ingested_at=None,
               sentiment=None, raw_score=None, event_at=None,
               event_hash=None):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(IntelEvent(
            symbol=symbol, asset_class="stock", source=source,
            headline=headline, url=url,
            sentiment=sentiment, raw_score=raw_score,
            ingested_at=ingested_at or now,
            event_at=event_at or now,
            event_hash=event_hash or f"h{abs(hash(url+headline))}",
        ))
        s.commit()


# ---------------------------------------------------------------------------
# URL dedup
# ---------------------------------------------------------------------------


def test_normalize_url_strips_scheme_query_fragment():
    assert adversarial._normalize_url("https://example.com/article?id=1#a") == "example.com/article"
    assert adversarial._normalize_url("http://example.com/x/") == "example.com/x"


def test_url_hash_stable_across_query_strings():
    h1 = adversarial.url_hash("https://example.com/article?id=1")
    h2 = adversarial.url_hash("https://example.com/article?id=2")
    h3 = adversarial.url_hash("http://example.com/article")
    assert h1 == h2 == h3


def test_url_hash_falls_back_to_headline_when_no_url():
    h1 = adversarial.url_hash("", "Same Headline")
    h2 = adversarial.url_hash("", "Same Headline")
    h3 = adversarial.url_hash("", "Different Headline")
    assert h1 == h2
    assert h1 != h3


def test_distinct_urls_dedups_across_sources():
    from types import SimpleNamespace
    fake = lambda url, headline: SimpleNamespace(url=url, headline=headline)
    events = [
        fake("https://example.com/a?utm=src1", "A"),
        fake("https://example.com/a?utm=src2", "A"),  # dup of first
        fake("https://example.com/b", "B"),
    ]
    hashes, n = adversarial.distinct_urls(events)
    assert n == 2


# ---------------------------------------------------------------------------
# Velocity / cold-start spike
# ---------------------------------------------------------------------------


def test_detect_spike_fires_on_cold_start_with_large_count(engine):
    """No prior history + 12 mentions this tick → spike fires."""
    out = adversarial.detect_suspicious_spike(
        engine, symbol="NEW", current_count=12, spike_threshold=10.0,
    )
    assert out is True


def test_detect_spike_no_fire_on_cold_start_below_threshold(engine):
    out = adversarial.detect_suspicious_spike(
        engine, symbol="NEW", current_count=3, spike_threshold=10.0,
    )
    assert out is False


def test_detect_spike_fires_when_above_median_multiple(engine):
    """5 prior days × 1 mention/day → median 1; current 15 → 15x spike."""
    now = dt.datetime.now(dt.timezone.utc)
    for d in range(1, 6):
        _add_event(engine, symbol="NVDA",
                   ingested_at=now - dt.timedelta(days=d, hours=12),
                   url=f"https://x/{d}", event_hash=f"d{d}")
    out = adversarial.detect_suspicious_spike(
        engine, symbol="NVDA", current_count=15, spike_threshold=10.0, now=now,
    )
    assert out is True


def test_detect_spike_no_fire_when_within_normal_range(engine):
    now = dt.datetime.now(dt.timezone.utc)
    # 5 days × 5 mentions/day → median 5; current 6 = 1.2x
    for d in range(1, 6):
        for n in range(5):
            _add_event(engine, symbol="NVDA",
                       ingested_at=now - dt.timedelta(days=d, hours=12),
                       url=f"https://x/{d}/{n}", event_hash=f"d{d}n{n}")
    out = adversarial.detect_suspicious_spike(
        engine, symbol="NVDA", current_count=6, spike_threshold=10.0, now=now,
    )
    assert out is False


# ---------------------------------------------------------------------------
# Coordination
# ---------------------------------------------------------------------------


def test_detect_coordinated_fires_on_3_sources_in_5min_cold_start(engine):
    now = dt.datetime.now(dt.timezone.utc)
    headline = "PUMP token announces partnership"
    for src in ("alpaca_news", "polygon_news", "yahoo_rss"):
        _add_event(engine, symbol="PUMP", source=src, headline=headline,
                   ingested_at=now - dt.timedelta(minutes=2),
                   url=f"https://x/{src}", event_hash=f"k{src}")
    out = adversarial.detect_coordinated(engine, symbol="PUMP", now=now)
    assert out is True


def test_detect_coordinated_no_fire_with_prior_history(engine):
    """If symbol had mentions in prior 24h, it's not cold-start."""
    now = dt.datetime.now(dt.timezone.utc)
    _add_event(engine, symbol="NVDA", source="polygon_news",
               headline="old story",
               ingested_at=now - dt.timedelta(hours=12),
               url="https://x/old", event_hash="oldhash")
    headline = "NVDA announces something"
    for src in ("alpaca_news", "polygon_news", "yahoo_rss"):
        _add_event(engine, symbol="NVDA", source=src, headline=headline,
                   ingested_at=now - dt.timedelta(minutes=2),
                   url=f"https://x/new/{src}", event_hash=f"new{src}")
    out = adversarial.detect_coordinated(engine, symbol="NVDA", now=now)
    assert out is False


def test_detect_coordinated_no_fire_with_distinct_headlines(engine):
    now = dt.datetime.now(dt.timezone.utc)
    for src, h in (
        ("alpaca_news", "A talks earnings"),
        ("polygon_news", "Different angle on A"),
        ("yahoo_rss", "Yet another A piece"),
    ):
        _add_event(engine, symbol="A", source=src, headline=h,
                   ingested_at=now - dt.timedelta(minutes=2),
                   url=f"https://x/{src}", event_hash=f"d{src}")
    out = adversarial.detect_coordinated(engine, symbol="A", now=now)
    assert out is False


# ---------------------------------------------------------------------------
# Pump signature
# ---------------------------------------------------------------------------


def test_pump_signature_fires_on_small_cap_social_no_news():
    sources = {"apewisdom": 60, "reddit_news": 20, "polygon_news": 0}
    assert adversarial.detect_pump_signature(
        symbol="GMER", sources_count=sources,
    ) is True


def test_pump_signature_no_fire_with_news():
    sources = {"apewisdom": 60, "reddit_news": 20, "polygon_news": 5}
    assert adversarial.detect_pump_signature(
        symbol="GMER", sources_count=sources,
    ) is False


def test_pump_signature_no_fire_below_social_floor():
    sources = {"apewisdom": 5, "reddit_news": 5, "polygon_news": 0}
    assert adversarial.detect_pump_signature(
        symbol="GMER", sources_count=sources,
    ) is False


def test_pump_signature_no_fire_for_crypto():
    sources = {"apewisdom": 100, "reddit_news": 50, "polygon_news": 0}
    # Crypto pairs contain '/'
    assert adversarial.detect_pump_signature(
        symbol="BTC/USD", sources_count=sources,
    ) is False


# ---------------------------------------------------------------------------
# Aggregator integration: flags persisted on IntelCandidate
# ---------------------------------------------------------------------------


def test_rollup_persists_adversarial_flags(engine):
    """Cold-start with 12 events should set suspicious_spike=True."""
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(12):
        _add_event(engine, symbol="PUMP", source="apewisdom",
                   headline=f"PUMP gaining traction {i}",
                   ingested_at=now,
                   url=f"https://x/p/{i}", event_hash=f"p{i}")
    summary = roll_up(engine, now=now)
    assert summary["candidates_upserted"] == 1
    with Session(engine) as s:
        row = s.query(IntelCandidate).filter(IntelCandidate.symbol == "PUMP").first()
    assert row.suspicious_spike is True


# ---------------------------------------------------------------------------
# Circuit breaker — state machine
# ---------------------------------------------------------------------------


def test_breaker_state_default_not_tripped(engine):
    assert circuit_breaker.is_tripped(engine) is False


def test_trip_then_state_returns_tripped(engine):
    circuit_breaker.trip(engine, reason="vix_spike", detail={"vix": 40},
                          cooldown_minutes=60)
    s = circuit_breaker.state(engine)
    assert s.tripped is True
    assert s.reason == "vix_spike"
    assert s.detail.get("vix") == 40


def test_clear_supersedes_trip(engine):
    circuit_breaker.trip(engine, reason="vix_spike")
    circuit_breaker.clear(engine, reason="manual")
    assert circuit_breaker.is_tripped(engine) is False


def test_trip_auto_clears_after_expiry(engine):
    """Most recent row is 'tripped' but expires_at < now → not tripped."""
    now = dt.datetime.now(dt.timezone.utc)
    past = now - dt.timedelta(minutes=10)
    circuit_breaker.trip(engine, reason="vix_spike", cooldown_minutes=5, now=past)
    assert circuit_breaker.is_tripped(engine) is False


# ---------------------------------------------------------------------------
# Circuit breaker — evaluate_metrics priority
# ---------------------------------------------------------------------------


def test_evaluate_no_trip_when_metrics_normal():
    out = circuit_breaker.evaluate_metrics(
        vix=15, daily_pnl_pct=0.5, consecutive_losses=0,
        fast_stops_count=0, api_error_rate=0.05,
    )
    assert out.should_trip is False


def test_evaluate_vix_spike_takes_top_priority():
    out = circuit_breaker.evaluate_metrics(
        vix=40, daily_pnl_pct=-5.0,  # both fire; VIX wins
    )
    assert out.should_trip is True
    assert out.reason == circuit_breaker.REASON_VIX_SPIKE


def test_evaluate_drawdown_fires_when_no_vix():
    out = circuit_breaker.evaluate_metrics(
        vix=15, daily_pnl_pct=-5.0, dd_threshold_pct=-3.0,
    )
    assert out.should_trip is True
    assert out.reason == circuit_breaker.REASON_DRAWDOWN


def test_evaluate_consecutive_losses_fires():
    out = circuit_breaker.evaluate_metrics(
        consecutive_losses=3, consecutive_losses_threshold=3,
    )
    assert out.should_trip is True
    assert out.reason == circuit_breaker.REASON_CONSECUTIVE_LOSSES


def test_evaluate_fast_stops_fires():
    out = circuit_breaker.evaluate_metrics(
        fast_stops_count=5, fast_stops_threshold=5,
    )
    assert out.should_trip is True
    assert out.reason == circuit_breaker.REASON_FAST_STOPS


def test_evaluate_api_error_rate_fires():
    out = circuit_breaker.evaluate_metrics(
        api_error_rate=0.6, api_error_rate_threshold=0.5,
    )
    assert out.should_trip is True
    assert out.reason == circuit_breaker.REASON_API_ERROR_RATE
