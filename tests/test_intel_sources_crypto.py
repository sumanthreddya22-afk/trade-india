"""Phase 6 — crypto-specific intel sources.

Tests cover:
  * Source weights/decays registered in aggregator.SOURCE_WEIGHTS / DECAY_HOURS.
  * RSS parser handles RSS 2.0 + Atom + missing fields.
  * Slug extraction picks longest-name-first (so "bitcoin cash" beats "bitcoin").
  * Each collector degrades gracefully on network outage (returns 0 written, no raise).
  * CryptoPanic skips silently when API key is empty.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trading_bot.intel import aggregator, sources
from trading_bot.intel._crypto_symbols import (
    NAME_TO_SYMBOL, extract_symbols_from_text,
)
from trading_bot.state_db import Base, IntelEvent, get_engine


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "state.db"
    eng = get_engine(db)
    Base.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Source weights / decays registered for the new crypto sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    ["apewisdom_crypto", "coindesk_rss", "cointelegraph_rss", "cryptopanic"],
)
def test_new_sources_have_weights(source):
    assert source in aggregator.SOURCE_WEIGHTS
    assert aggregator.SOURCE_WEIGHTS[source] > 0
    assert source in aggregator.DECAY_HOURS
    assert aggregator.DECAY_HOURS[source] > 0


def test_editorial_sources_outweigh_aggregator_decay():
    """CoinDesk / CoinTelegraph editorial decays slower than the
    cryptopanic aggregator (same order as commercial-news vs feed)."""
    assert aggregator.DECAY_HOURS["coindesk_rss"] >= aggregator.DECAY_HOURS["cryptopanic"]


# ---------------------------------------------------------------------------
# Slug → symbol extraction
# ---------------------------------------------------------------------------


def test_extract_symbols_basic_canonical_names():
    out = extract_symbols_from_text("Bitcoin and Ethereum hit new highs")
    assert "BTC" in out and "ETH" in out


def test_extract_symbols_longest_match_first():
    """'bitcoin cash' must match BCH, not just BTC from 'bitcoin'."""
    out = extract_symbols_from_text("Bitcoin Cash forks again")
    assert "BCH" in out
    assert "BTC" not in out


def test_extract_symbols_dollar_ticker():
    out = extract_symbols_from_text("Big move on $SOL today")
    assert "SOL" in out


def test_extract_symbols_unknown_dollar_tickers_dropped():
    """Equity tickers in mixed feeds shouldn't pollute the crypto pool."""
    out = extract_symbols_from_text("$AAPL and $MSFT had earnings")
    assert "AAPL" not in out
    assert "MSFT" not in out


def test_extract_symbols_empty_text():
    assert extract_symbols_from_text("") == []
    assert extract_symbols_from_text(None) == []


def test_extract_symbols_word_boundary():
    """'bitcoin' inside another word doesn't match."""
    out = extract_symbols_from_text("bitcoinmagazine.com analysis")
    assert "BTC" not in out


# ---------------------------------------------------------------------------
# Collectors — failure isolation
# ---------------------------------------------------------------------------


def test_apewisdom_crypto_no_data_returns_zero(engine):
    with patch(
        "trading_bot.intel_gates._fetch_crypto_mentions", return_value={},
    ):
        out = sources.collect_apewisdom_crypto(engine)
    assert out["source"] == "apewisdom_crypto"
    assert out["written"] == 0


def test_apewisdom_crypto_writes_only_above_threshold(engine):
    """Mentions ≥ 5 are written; below threshold dropped (matches equity collector)."""
    snap = {
        "BTC": {"mentions": 100, "mentions_24h_ago": 50, "rank": 1},
        "ETH": {"mentions": 80, "mentions_24h_ago": 40, "rank": 2},
        "WIF": {"mentions": 3, "mentions_24h_ago": 1, "rank": 50},  # below 5 threshold
    }
    with patch(
        "trading_bot.intel_gates._fetch_crypto_mentions", return_value=snap,
    ):
        out = sources.collect_apewisdom_crypto(engine)
    assert out["written"] == 2  # BTC + ETH only


def test_apewisdom_crypto_swallows_exceptions(engine):
    with patch(
        "trading_bot.intel_gates._fetch_crypto_mentions",
        side_effect=ConnectionError("rate limit"),
    ):
        out = sources.collect_apewisdom_crypto(engine)
    assert out["written"] == 0
    assert "error" in out


def test_rss_parser_handles_rss20():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Test</title>
        <item>
            <title>Bitcoin hits new ATH</title>
            <link>https://example.com/1</link>
            <pubDate>Wed, 02 May 2026 12:00:00 +0000</pubDate>
            <description>BTC price soars on ETF inflows</description>
        </item>
        <item>
            <title>Ethereum upgrade scheduled</title>
            <link>https://example.com/2</link>
            <pubDate>Wed, 02 May 2026 13:00:00 +0000</pubDate>
            <description>ETH dev call confirms timeline</description>
        </item>
    </channel></rss>"""
    out = sources._parse_rss_entries(xml)
    assert len(out) == 2
    assert out[0]["title"] == "Bitcoin hits new ATH"
    assert out[1]["link"] == "https://example.com/2"


def test_rss_parser_handles_atom():
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Solana TVL up</title>
                <link href="https://example.com/sol"/>
                <published>2026-05-02T10:00:00Z</published>
                <summary>SOL DeFi surge</summary>
            </entry>
        </feed>"""
    out = sources._parse_rss_entries(xml)
    assert len(out) == 1
    assert out[0]["title"] == "Solana TVL up"
    assert out[0]["link"] == "https://example.com/sol"


def test_rss_parser_returns_empty_on_garbage():
    assert sources._parse_rss_entries(b"<<<not xml>>>") == []
    assert sources._parse_rss_entries(b"") == []


def test_coindesk_rss_writes_per_symbol_event(engine):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item>
            <title>Bitcoin breaks $70k as ETF inflows accelerate</title>
            <link>https://www.coindesk.com/article-1</link>
            <pubDate>Wed, 02 May 2026 12:00:00 +0000</pubDate>
            <description>BTC rally continues</description>
        </item>
    </channel></rss>"""
    fake_response = MagicMock()
    fake_response.content = rss
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_coindesk_rss(engine)
    assert out["source"] == "coindesk_rss"
    assert out["written"] >= 1
    # Verify the row landed
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(
            IntelEvent.source == "coindesk_rss"
        ).all()
        assert len(rows) >= 1
        assert any(r.symbol == "BTC/USD" for r in rows)


def test_coindesk_rss_drops_articles_with_no_known_symbols(engine):
    """Headline mentioning only NFT or unknown coin → no events written."""
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item>
            <title>Some random NFT collection mints out</title>
            <link>https://www.coindesk.com/article-x</link>
            <pubDate>Wed, 02 May 2026 12:00:00 +0000</pubDate>
        </item>
    </channel></rss>"""
    fake_response = MagicMock()
    fake_response.content = rss
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_coindesk_rss(engine)
    assert out["written"] == 0


def test_coindesk_rss_swallows_network_error(engine):
    with patch("requests.get", side_effect=ConnectionError("dns")):
        out = sources.collect_coindesk_rss(engine)
    assert out["written"] == 0
    assert "error" in out


def test_cryptopanic_skipped_when_no_key(engine):
    settings = MagicMock()
    settings.cryptopanic_api_key = ""
    out = sources.collect_cryptopanic(engine, settings=settings)
    assert out["written"] == 0
    assert "no api key" in out.get("note", "")


def test_cryptopanic_writes_per_currency(engine):
    settings = MagicMock()
    settings.cryptopanic_api_key = "test_key"
    body = {
        "results": [
            {
                "title": "BTC and ETH rally on macro shift",
                "url": "https://cryptopanic.com/news/1",
                "published_at": "2026-05-02T10:00:00Z",
                "votes": {"positive": 80, "negative": 20, "important": 5},
                "currencies": [{"code": "BTC"}, {"code": "ETH"}],
            },
        ],
    }
    fake_response = MagicMock()
    fake_response.json = lambda: body
    fake_response.raise_for_status = MagicMock()
    with patch("requests.get", return_value=fake_response):
        out = sources.collect_cryptopanic(engine, settings=settings)
    assert out["written"] == 2  # one per currency
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        rows = s.query(IntelEvent).filter(
            IntelEvent.source == "cryptopanic"
        ).all()
        symbols = {r.symbol for r in rows}
        assert "BTC/USD" in symbols
        assert "ETH/USD" in symbols
        # Sentiment normalised to (pos - neg) / (pos + neg) = 60/100 = 0.6
        for r in rows:
            assert r.sentiment is not None
            assert 0.5 < r.sentiment <= 1.0


def test_cryptopanic_swallows_network_error(engine):
    settings = MagicMock()
    settings.cryptopanic_api_key = "test_key"
    with patch("requests.get", side_effect=ConnectionError("dns")):
        out = sources.collect_cryptopanic(engine, settings=settings)
    assert out["written"] == 0
    assert "error" in out


# ---------------------------------------------------------------------------
# collect_all registration
# ---------------------------------------------------------------------------


def test_collect_all_includes_new_sources(engine):
    """All four crypto collectors must be wired into the orchestrator path."""
    settings = MagicMock()
    settings.cryptopanic_api_key = ""
    # Stub every collector to a no-op return so we just count the source slots
    with patch.multiple(
        sources,
        collect_alpaca_news=MagicMock(return_value={"source": "alpaca_news", "written": 0, "skipped": 0}),
        collect_sec_form4=MagicMock(return_value={"source": "sec_form4", "written": 0, "skipped": 0}),
        collect_apewisdom=MagicMock(return_value={"source": "apewisdom", "written": 0, "skipped": 0}),
        collect_vip_tweets=MagicMock(return_value={"source": "vip_tweet", "written": 0, "skipped": 0}),
        collect_finnhub_news=MagicMock(return_value={"source": "finnhub_news", "written": 0, "skipped": 0}),
        collect_gdelt=MagicMock(return_value={"source": "gdelt", "written": 0, "skipped": 0}),
        collect_apewisdom_crypto=MagicMock(return_value={"source": "apewisdom_crypto", "written": 0, "skipped": 0}),
        collect_coindesk_rss=MagicMock(return_value={"source": "coindesk_rss", "written": 0, "skipped": 0}),
        collect_cointelegraph_rss=MagicMock(return_value={"source": "cointelegraph_rss", "written": 0, "skipped": 0}),
        collect_cryptopanic=MagicMock(return_value={"source": "cryptopanic", "written": 0, "skipped": 0}),
    ):
        out = sources.collect_all(engine, settings=settings, seed_symbols=[])
    sources_seen = {r["source"] for r in out}
    for required in (
        "apewisdom_crypto", "coindesk_rss", "cointelegraph_rss", "cryptopanic",
    ):
        assert required in sources_seen
