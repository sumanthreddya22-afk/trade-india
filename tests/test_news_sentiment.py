from datetime import date, datetime, timedelta, timezone

from trading_bot.news_sentiment import (
    SentimentCache,
    SentimentReading,
    passes_filter,
    score_for,
)


def test_cache_roundtrip(tmp_path):
    c = SentimentCache(tmp_path / "ns.db")
    r = SentimentReading(
        symbol="AAPL", snapshot_date=date(2026, 4, 26),
        score=0.5, n_articles=10, dominant_label="positive",
    )
    c.write(r)
    out = c.latest("AAPL", max_age_days=30)
    assert out is not None
    assert out.symbol == "AAPL"
    assert out.score == 0.5
    assert out.dominant_label == "positive"


def test_cache_returns_none_when_too_old(tmp_path):
    c = SentimentCache(tmp_path / "ns.db")
    old = SentimentReading(
        symbol="AAPL",
        snapshot_date=(datetime.now(timezone.utc) - timedelta(days=30)).date(),
        score=0.5, n_articles=10, dominant_label="positive",
    )
    c.write(old)
    assert c.latest("AAPL", max_age_days=7) is None


def test_passes_filter_disabled_when_floor_none():
    assert passes_filter(-0.9, floor=None) is True
    assert passes_filter(None, floor=None) is True


def test_passes_filter_pass_when_no_data():
    """No data shouldn't veto; filter should only block on EXPLICIT negativity."""
    assert passes_filter(None, floor=-0.3) is True


def test_passes_filter_blocks_below_floor():
    assert passes_filter(-0.5, floor=-0.3) is False
    assert passes_filter(-0.3, floor=-0.3) is True
    assert passes_filter(0.0, floor=-0.3) is True
    assert passes_filter(0.7, floor=-0.3) is True


def test_score_for_returns_none_on_missing(tmp_path):
    c = SentimentCache(tmp_path / "ns.db")
    assert score_for("XYZ", cache=c) is None
