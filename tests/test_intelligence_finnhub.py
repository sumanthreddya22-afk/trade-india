import datetime as dt
from unittest.mock import patch, MagicMock
import pytest
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable


def _resp(json_body, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_body
    m.raise_for_status.return_value = None
    return m


def test_earnings_calendar_returns_normalized_rows():
    body = {"earningsCalendar": [
        {"symbol": "AAPL", "date": "2026-05-02", "epsActual": None, "epsEstimate": 1.5},
        {"symbol": "MSFT", "date": "2026-05-03", "epsActual": None, "epsEstimate": 2.7},
    ]}
    c = FinnhubClient(api_key="k")
    with patch("requests.get", return_value=_resp(body)) as g:
        out = c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8))
    assert len(out) == 2
    assert out[0].symbol == "AAPL" and out[0].date == dt.date(2026, 5, 2)
    g.assert_called_once()


def test_earnings_calendar_returns_empty_when_no_key():
    c = FinnhubClient(api_key="")
    assert c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8)) == []


def test_company_profile_caches_and_returns():
    body = {"marketCapitalization": 2500.0, "ipo": "1986-03-13", "exchange": "NASDAQ"}
    c = FinnhubClient(api_key="k")
    with patch("requests.get", return_value=_resp(body)) as g:
        a = c.company_profile("MSFT")
        b = c.company_profile("MSFT")
    assert a == b
    assert a.market_cap_musd == 2500.0
    assert g.call_count == 1  # cache hit on second call


def test_raises_finnhub_unavailable_on_500():
    c = FinnhubClient(api_key="k")
    bad = MagicMock()
    bad.status_code = 503
    bad.raise_for_status.side_effect = Exception("server error")
    with patch("requests.get", return_value=bad):
        with pytest.raises(FinnhubUnavailable):
            c.earnings_calendar(dt.date(2026, 5, 1), dt.date(2026, 5, 8))
