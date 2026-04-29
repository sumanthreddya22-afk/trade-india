from unittest.mock import patch, MagicMock
from trading_bot.intelligence_apewisdom import ApeWisdomClient


def _resp(body):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = body
    m.raise_for_status.return_value = None
    return m


def test_returns_normalized_mentions():
    body = {"results": [
        {"ticker": "GME", "mentions": 800, "mentions_24h_ago": 200, "rank": 1},
        {"ticker": "AAPL", "mentions": 50, "mentions_24h_ago": 60, "rank": 22},
    ]}
    c = ApeWisdomClient()
    with patch("requests.get", return_value=_resp(body)):
        out = c.wallstreetbets_mentions()
    assert out["GME"].mentions == 800 and out["GME"].rank == 1
    assert out["AAPL"].mentions == 50


def test_is_spike_detects_high_growth():
    body = {"results": [{"ticker": "GME", "mentions": 800, "mentions_24h_ago": 200, "rank": 1}]}
    c = ApeWisdomClient()
    with patch("requests.get", return_value=_resp(body)):
        c.wallstreetbets_mentions()
    assert c.is_spike("GME", multiplier=2.0) is True
    assert c.is_spike("AAPL", multiplier=2.0) is False  # not loaded


def test_returns_empty_on_network_error():
    c = ApeWisdomClient()
    with patch("requests.get", side_effect=Exception("boom")):
        assert c.wallstreetbets_mentions() == {}
