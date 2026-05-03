"""Phase A — diversified stock-news intel sources.

Tests cover:
  * Source weights/decays registered in aggregator.SOURCE_WEIGHTS / DECAY_HOURS
  * SEC CIK map refresh + lookup + ticker→CIK build
  * Polygon news collector: sentiment mapping, error/empty handling
  * SEC 8-K collector: item parsing, sentiment from item type, no-CIK skip
  * Yahoo / GoogleNews RSS: per-symbol fetch, error swallowing
  * Reddit broader subs: score/comment filter, ticker extraction
  * NewsAPI: daily-window gate, empty-key skip
  * Elevated-symbols helper: filter by score + asset_class
  * collect_all() sequencing: legacy + new + crypto sources all called
"""
from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.intel import aggregator, sources
from trading_bot.intel import sec_cik_map
from trading_bot.state_db import (
    Base, IntelCandidate, IntelEvent, get_engine,
)


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Source weights / decays registered for the new stock sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["polygon_news", "yahoo_rss", "googlenews_rss", "reddit_news", "newsapi"],
)
def test_phase_a_sources_have_weights(source):
    assert source in aggregator.SOURCE_WEIGHTS
    assert aggregator.SOURCE_WEIGHTS[source] > 0
    assert source in aggregator.DECAY_HOURS
    assert aggregator.DECAY_HOURS[source] > 0


def test_sec_8k_outweighs_other_sources():
    """SEC 8-K is the highest-trust free source — its weight must dominate."""
    others = ["polygon_news", "alpaca_news", "yahoo_rss",
              "googlenews_rss", "reddit_news", "newsapi"]
    for s in others:
        assert aggregator.SOURCE_WEIGHTS["sec_8k"] > aggregator.SOURCE_WEIGHTS[s]


def test_polygon_news_decay_matches_other_news_sources():
    """Polygon news is editorial-grade like alpaca/finnhub."""
    assert aggregator.DECAY_HOURS["polygon_news"] == aggregator.DECAY_HOURS["alpaca_news"]


# ---------------------------------------------------------------------------
# SEC CIK map
# ---------------------------------------------------------------------------


def test_build_ticker_to_cik_zero_pads_cik():
    raw = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }
    out = sec_cik_map._build_ticker_to_cik(raw)
    assert out["AAPL"] == "0000320193"
    assert out["MSFT"] == "0000789019"


def test_build_ticker_to_cik_skips_malformed_rows():
    raw = {
        "0": {"cik_str": 320193, "ticker": "AAPL"},
        "1": {"cik_str": None, "ticker": "BAD"},      # missing CIK
        "2": {"cik_str": 999, "ticker": ""},          # empty ticker
        "3": "not a dict",                             # not a dict
    }
    out = sec_cik_map._build_ticker_to_cik(raw)
    assert out == {"AAPL": "0000320193"}


def test_refresh_cik_map_uses_cache_when_fresh(tmp_path):
    sec_cik_map.reset_cache()
    cache = tmp_path / "cik.json"
    cache.write_text(json.dumps({"0": {"cik_str": 1, "ticker": "X"}}))
    with patch("trading_bot.intel.sec_cik_map.requests.get") as get:
        out = sec_cik_map.refresh_cik_map(path=cache)
    get.assert_not_called()  # cache is fresh
    assert out == {"X": "0000000001"}


def test_refresh_cik_map_falls_back_to_stale_on_network_failure(tmp_path):
    sec_cik_map.reset_cache()
    cache = tmp_path / "cik.json"
    cache.write_text(json.dumps({"0": {"cik_str": 7, "ticker": "Y"}}))
    # Simulate stale by setting mtime far in the past
    import os
    old = dt.datetime.now().timestamp() - 30 * 86400
    os.utime(cache, (old, old))
    with patch(
        "trading_bot.intel.sec_cik_map.requests.get",
        side_effect=ConnectionError("dns"),
    ):
        out = sec_cik_map.refresh_cik_map(path=cache)
    # Stale cache still readable
    assert out == {"Y": "0000000007"}


# ---------------------------------------------------------------------------
# Polygon News collector
# ---------------------------------------------------------------------------


def _make_polygon_article(*, ticker, title, sentiment_label="positive", url="https://x"):
    from trading_bot.massive_client import NewsArticle
    return NewsArticle(
        article_id="a1", publisher="Pub",
        title=title, url=url,
        published_utc=dt.datetime(2026, 5, 2, 12, 0, 0, tzinfo=dt.timezone.utc),
        tickers=(ticker,),
        description="",
        sentiments={ticker: sentiment_label},
        sentiment_reasons={ticker: "test"},
    )


def test_polygon_news_skipped_when_no_key(engine):
    settings = MagicMock()
    settings.polygon_api_key = ""
    out = sources.collect_massive_news(engine, settings=settings, symbols=["AAPL"])
    assert out["written"] == 0
    assert out.get("note") == "no api key"


def test_polygon_news_skipped_when_no_symbols(engine):
    settings = MagicMock()
    settings.polygon_api_key = "key"
    out = sources.collect_massive_news(engine, settings=settings, symbols=[])
    assert out["written"] == 0
    assert "no symbols" in out.get("note", "")


def test_polygon_news_writes_with_mapped_sentiment(engine):
    settings = MagicMock()
    settings.polygon_api_key = "key"
    fake_client = MagicMock()
    fake_client.news.return_value = [
        _make_polygon_article(ticker="AAPL", title="Apple beats Q3", sentiment_label="positive", url="https://x/1"),
        _make_polygon_article(ticker="AAPL", title="Apple guidance lowered", sentiment_label="negative", url="https://x/2"),
    ]
    with patch("trading_bot.massive_client.MassiveClient", return_value=fake_client):
        out = sources.collect_massive_news(engine, settings=settings, symbols=["AAPL"])
    assert out["written"] == 2
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(IntelEvent.source == "polygon_news").all()
    sentiments = {r.headline: r.sentiment for r in rows}
    assert sentiments["Apple beats Q3"] == 0.6
    assert sentiments["Apple guidance lowered"] == -0.6


def test_polygon_news_swallows_per_symbol_errors(engine):
    settings = MagicMock()
    settings.polygon_api_key = "key"
    fake_client = MagicMock()
    fake_client.news.side_effect = ConnectionError("dns")
    with patch("trading_bot.massive_client.MassiveClient", return_value=fake_client):
        out = sources.collect_massive_news(engine, settings=settings, symbols=["AAPL", "MSFT"])
    assert out["written"] == 0
    assert out.get("errors", 0) >= 1


def test_polygon_news_stops_batch_on_rate_limit(engine):
    """Once Polygon rate-limits, no point trying remaining symbols this tick."""
    from trading_bot.massive_client import MassiveRateLimitError
    settings = MagicMock()
    settings.polygon_api_key = "key"
    fake_client = MagicMock()
    fake_client.news.side_effect = MassiveRateLimitError("429")
    with patch("trading_bot.massive_client.MassiveClient", return_value=fake_client):
        out = sources.collect_massive_news(
            engine, settings=settings, symbols=["AAPL", "MSFT", "NVDA"],
        )
    # Only one call attempted (then break)
    assert fake_client.news.call_count == 1
    assert out["written"] == 0


# ---------------------------------------------------------------------------
# SEC 8-K collector
# ---------------------------------------------------------------------------


def test_parse_8k_items_single():
    out = sources._parse_8k_items("8-K Item 2.02 Results of Operations")
    assert out == ["2.02"]


def test_parse_8k_items_multiple_comma_separated():
    out = sources._parse_8k_items("Items 2.02, 7.01 - earnings + Reg FD")
    assert sorted(out) == ["2.02", "7.01"]


def test_parse_8k_items_returns_empty_when_none():
    assert sources._parse_8k_items("Just a title") == []


def test_sec_8k_sentiment_takes_worst_case():
    """Item 2.02 (neutral) bundled with Item 2.06 (-0.8) → worst-case bias picks -0.8."""
    out = sources._sec_8k_sentiment_from_items(["2.02", "2.06"])
    assert out == -0.8


def test_sec_8k_sentiment_returns_none_on_unknown_items():
    assert sources._sec_8k_sentiment_from_items(["99.99"]) is None
    assert sources._sec_8k_sentiment_from_items([]) is None


def test_sec_8k_writes_with_item_sentiment(engine):
    sec_cik_map.reset_cache()
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>8-K - Item 2.06 Material Impairment</title>
                <link href="https://www.sec.gov/cgi-bin/browse-edgar?...&amp;Filing=1"/>
                <updated>2026-05-02T10:00:00Z</updated>
                <summary>Material impairment recorded</summary>
            </entry>
        </feed>"""
    fake_response = MagicMock()
    fake_response.content = atom
    fake_response.raise_for_status = MagicMock()
    with patch(
        "trading_bot.intel.sec_cik_map.get_cik_for", return_value="0000320193",
    ), patch("requests.get", return_value=fake_response):
        out = sources.collect_sec_8k(engine, symbols=["AAPL"])
    assert out["written"] == 1
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(IntelEvent.source == "sec_8k").all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].sentiment == -0.8  # Item 2.06 → worst-case score


def test_sec_8k_skips_when_no_cik(engine):
    """Tickers we can't map to a CIK are counted, not failed."""
    with patch(
        "trading_bot.intel.sec_cik_map.get_cik_for", return_value=None,
    ):
        out = sources.collect_sec_8k(engine, symbols=["UNKNOWN"])
    assert out["written"] == 0
    assert out.get("no_cik") == 1


def test_sec_8k_swallows_network_errors(engine):
    with patch(
        "trading_bot.intel.sec_cik_map.get_cik_for", return_value="0000000001",
    ), patch("requests.get", side_effect=ConnectionError("dns")):
        out = sources.collect_sec_8k(engine, symbols=["AAPL"])
    assert out["written"] == 0
    assert out.get("errors", 0) >= 1


def test_sec_8k_no_symbols_returns_zero(engine):
    out = sources.collect_sec_8k(engine, symbols=[])
    assert out["written"] == 0


# ---------------------------------------------------------------------------
# Yahoo / GoogleNews RSS
# ---------------------------------------------------------------------------


def _yahoo_rss_xml() -> bytes:
    return b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item>
            <title>Apple Q3 results beat estimates</title>
            <link>https://finance.yahoo.com/news/apple-1</link>
            <pubDate>Wed, 02 May 2026 12:00:00 +0000</pubDate>
            <description>AAPL beats</description>
        </item>
        <item>
            <title>Analyst raises Apple price target</title>
            <link>https://finance.yahoo.com/news/apple-2</link>
            <pubDate>Wed, 02 May 2026 13:00:00 +0000</pubDate>
            <description>Bullish analyst note</description>
        </item>
    </channel></rss>"""


def test_yahoo_rss_writes_per_symbol(engine):
    fake_response = MagicMock()
    fake_response.content = _yahoo_rss_xml()
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_yahoo_rss(engine, symbols=["AAPL"])
    assert out["source"] == "yahoo_rss"
    assert out["written"] == 2
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(IntelEvent.source == "yahoo_rss").all()
    assert {r.symbol for r in rows} == {"AAPL"}


def test_yahoo_rss_no_symbols_returns_zero(engine):
    out = sources.collect_yahoo_rss(engine, symbols=[])
    assert out["written"] == 0


def test_yahoo_rss_swallows_network_errors(engine):
    with patch("requests.get", side_effect=ConnectionError("dns")):
        out = sources.collect_yahoo_rss(engine, symbols=["AAPL"])
    assert out["written"] == 0
    assert out.get("errors", 0) >= 1


def test_googlenews_rss_writes_per_symbol(engine):
    fake_response = MagicMock()
    fake_response.content = _yahoo_rss_xml()  # same RSS shape
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_googlenews_rss(engine, symbols=["AAPL"])
    assert out["source"] == "googlenews_rss"
    assert out["written"] == 2


# ---------------------------------------------------------------------------
# Reddit broader subs
# ---------------------------------------------------------------------------


def _reddit_listing(*, score: int = 50, n_comments: int = 30, title: str = "$AAPL earnings",
                    permalink: str = "/r/stocks/comments/abc/foo"):
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "score": score,
                        "num_comments": n_comments,
                        "title": title,
                        "selftext": "",
                        "permalink": permalink,
                        "created_utc": 1714650000.0,
                    },
                },
            ],
        },
    }


def _reddit_responses_per_sub(score: int = 50, n_comments: int = 30, title: str = "$AAPL crushes Q3"):
    """Build 3 distinct fake responses (one per sub) so write_event's
    URL-based dedup doesn't collapse them into a single event."""
    out = []
    for sub in ("stocks", "investing", "options"):
        body = _reddit_listing(
            score=score, n_comments=n_comments, title=title,
            permalink=f"/r/{sub}/comments/abc/foo",
        )
        resp = MagicMock()
        resp.json = lambda b=body: b
        resp.raise_for_status = MagicMock()
        out.append(resp)
    return out


def test_reddit_news_writes_when_score_above_floor(engine):
    settings = MagicMock()
    settings.reddit_user_agent = "TestBot"
    with patch("requests.get", side_effect=_reddit_responses_per_sub()):
        out = sources.collect_reddit_news(engine, settings=settings)
    # 3 subs × 1 post × 1 ticker each = 3 writes
    assert out["written"] == 3


def test_reddit_news_filters_low_engagement(engine):
    """Posts with score <10 AND comments <20 are dropped."""
    settings = MagicMock()
    settings.reddit_user_agent = "TestBot"
    fake_response = MagicMock()
    fake_response.json = lambda: _reddit_listing(score=2, n_comments=3, title="$AAPL random thought")
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_reddit_news(engine, settings=settings)
    assert out["written"] == 0


def test_reddit_news_skips_posts_without_tickers(engine):
    settings = MagicMock()
    settings.reddit_user_agent = "TestBot"
    fake_response = MagicMock()
    fake_response.json = lambda: _reddit_listing(score=100, title="General market thoughts today")
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_reddit_news(engine, settings=settings)
    assert out["written"] == 0


def test_reddit_news_swallows_network_errors(engine):
    settings = MagicMock()
    settings.reddit_user_agent = "TestBot"
    with patch("requests.get", side_effect=ConnectionError("dns")):
        out = sources.collect_reddit_news(engine, settings=settings)
    assert out["written"] == 0
    assert out.get("errors", 0) >= 1


# ---------------------------------------------------------------------------
# NewsAPI
# ---------------------------------------------------------------------------


def test_newsapi_skipped_outside_daily_window(engine):
    """Default behaviour: only one tick/day fires NewsAPI."""
    settings = MagicMock()
    settings.newsapi_key = "key"
    # Pick an hour we know is NOT the gate hour.
    off_window = dt.datetime(2026, 5, 2, 0, 0, 0, tzinfo=dt.timezone.utc)
    with patch(
        "trading_bot.intel.sources.dt.datetime"
    ) as fake_dt:
        fake_dt.now.return_value = off_window
        fake_dt.timezone = dt.timezone
        fake_dt.timedelta = dt.timedelta
        out = sources.collect_newsapi(engine, settings=settings, symbols=["AAPL"])
    assert "outside daily window" in out.get("note", "")
    assert out["written"] == 0


def test_newsapi_skipped_when_no_key(engine):
    settings = MagicMock()
    settings.newsapi_key = ""
    out = sources.collect_newsapi(engine, settings=settings, symbols=["AAPL"], force=True)
    assert out["written"] == 0
    assert out.get("note") == "no api key"


def test_newsapi_writes_when_forced(engine):
    settings = MagicMock()
    settings.newsapi_key = "key"
    body = {
        "articles": [
            {"title": "Apple announces buyback", "url": "https://x/1",
             "publishedAt": "2026-05-02T10:00:00Z"},
            {"title": "Apple iPhone sales rise", "url": "https://x/2",
             "publishedAt": "2026-05-02T11:00:00Z"},
        ],
    }
    fake_response = MagicMock()
    fake_response.json = lambda: body
    fake_response.status_code = 200
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_newsapi(
            engine, settings=settings, symbols=["AAPL"], force=True,
        )
    assert out["written"] == 2


def test_newsapi_stops_on_rate_limit(engine):
    settings = MagicMock()
    settings.newsapi_key = "key"
    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_newsapi(
            engine, settings=settings, symbols=["AAPL", "MSFT", "NVDA"], force=True,
        )
    assert out["written"] == 0
    assert out.get("errors", 0) >= 1


# ---------------------------------------------------------------------------
# _elevated_symbols helper
# ---------------------------------------------------------------------------


def test_elevated_symbols_filters_by_score(engine):
    from sqlalchemy.orm import Session
    now = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add_all([
            IntelCandidate(
                symbol="HIGH", asset_class="stock", score=8.0,
                n_mentions=3, n_sources=2, sources_json="{}",
                first_seen=now, last_seen=now, top_reason="x",
                rolled_up_at=now,
            ),
            IntelCandidate(
                symbol="LOW", asset_class="stock", score=0.5,
                n_mentions=1, n_sources=1, sources_json="{}",
                first_seen=now, last_seen=now, top_reason="x",
                rolled_up_at=now,
            ),
            IntelCandidate(
                symbol="BTC/USD", asset_class="crypto", score=9.0,
                n_mentions=5, n_sources=3, sources_json="{}",
                first_seen=now, last_seen=now, top_reason="x",
                rolled_up_at=now,
            ),
        ])
        s.commit()
    elev = sources._elevated_symbols(engine, asset_class="stock", min_score=2.0)
    assert "HIGH" in elev
    assert "LOW" not in elev
    assert "BTC/USD" not in elev  # wrong asset class


def test_elevated_symbols_returns_empty_on_cold_start(engine):
    elev = sources._elevated_symbols(engine, asset_class="stock", min_score=2.0)
    assert elev == []


# ---------------------------------------------------------------------------
# collect_all sequencing
# ---------------------------------------------------------------------------


def test_collect_all_includes_phase_a_sources(engine):
    """Sanity: collect_all() returns a result for every Phase-A source."""
    settings = MagicMock()
    settings.polygon_api_key = ""
    settings.newsapi_key = ""
    settings.cryptopanic_api_key = ""
    settings.reddit_user_agent = "TestBot"
    # Stub all network calls to return a minimal valid response so all
    # collectors run and emit a per-source summary.
    fake_resp = MagicMock()
    fake_resp.content = b"<rss><channel></channel></rss>"
    fake_resp.json = lambda: {"results": [], "articles": [], "data": {"children": []}}
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_resp), \
         patch("trading_bot.intelligence.AlpacaNews"), \
         patch("trading_bot.intelligence_apewisdom.ApeWisdomClient"), \
         patch("trading_bot.intel_gates._fetch_crypto_mentions", return_value={}), \
         patch("trading_bot.vip_tweets.load_handles", return_value=[]):
        out = sources.collect_all(engine, settings=settings, seed_symbols=["AAPL"])
    sources_seen = {r["source"] for r in out}
    # Phase A sources MUST appear
    for s in ("sec_8k", "polygon_news", "yahoo_rss",
              "googlenews_rss", "reddit_news", "newsapi"):
        assert s in sources_seen, f"missing {s} in collect_all output"


def test_collect_all_keeps_legacy_and_crypto_sources(engine):
    """Phase A must NOT remove legacy or crypto sources from collect_all."""
    settings = MagicMock()
    settings.polygon_api_key = ""
    settings.newsapi_key = ""
    settings.cryptopanic_api_key = ""
    settings.reddit_user_agent = "TestBot"
    fake_resp = MagicMock()
    fake_resp.content = b"<rss><channel></channel></rss>"
    fake_resp.json = lambda: {"results": [], "articles": [], "data": {"children": []}}
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_resp), \
         patch("trading_bot.intelligence.AlpacaNews"), \
         patch("trading_bot.intelligence_apewisdom.ApeWisdomClient"), \
         patch("trading_bot.intel_gates._fetch_crypto_mentions", return_value={}), \
         patch("trading_bot.vip_tweets.load_handles", return_value=[]):
        out = sources.collect_all(engine, settings=settings, seed_symbols=["AAPL"])
    sources_seen = {r["source"] for r in out}
    legacy_and_crypto = {
        "alpaca_news", "sec_form4", "finnhub_news", "gdelt", "apewisdom",
        "vip_tweet", "apewisdom_crypto", "coindesk_rss", "cointelegraph_rss",
        "cryptopanic",
    }
    missing = legacy_and_crypto - sources_seen
    assert not missing, f"collect_all dropped legacy/crypto sources: {missing}"
