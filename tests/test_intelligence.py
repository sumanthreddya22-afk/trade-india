from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from trading_bot.intelligence import (
    GdeltEvent,
    InsiderFiling,
    MacroSnapshot,
    NewsItem,
    get_macro_snapshot,
    get_gdelt_events,
    get_recent_insider_filings,
)


def test_macro_snapshot_handles_empty(monkeypatch):
    def fake_fetch(series):
        return None
    monkeypatch.setattr("trading_bot.intelligence._fred_latest", fake_fetch)
    snap = get_macro_snapshot()
    assert isinstance(snap, MacroSnapshot)
    assert snap.vix is None
    assert snap.yield_10y_pct is None


def test_macro_snapshot_returns_floats(monkeypatch):
    monkeypatch.setattr("trading_bot.intelligence._fred_latest",
                        lambda series: {"VIXCLS": 19.5, "DGS10": 4.3, "DFF": 3.6}.get(series))
    snap = get_macro_snapshot()
    assert snap.vix == 19.5
    assert snap.yield_10y_pct == 4.3
    assert snap.fed_funds_pct == 3.6


def test_gdelt_handles_failure(monkeypatch):
    def fake_get(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr("trading_bot.intelligence.requests.get", fake_get)
    assert get_gdelt_events() == []


def test_gdelt_parses_articles(monkeypatch):
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "articles": [
            {"title": "Stocks rally", "url": "http://x.com/1",
             "seendate": "2026-04-25T12:00:00Z", "sourcecountry": "US", "tone": "2.5"},
            {"title": "Fed signals cut", "url": "http://x.com/2",
             "seendate": "2026-04-25T11:00:00Z", "sourcecountry": "US", "tone": "1.0"},
        ]
    }
    monkeypatch.setattr("trading_bot.intelligence.requests.get", lambda *a, **kw: fake_response)
    events = get_gdelt_events()
    assert len(events) == 2
    assert events[0].title == "Stocks rally"
    assert events[0].sentiment == 2.5


def test_insider_handles_failure(monkeypatch):
    monkeypatch.setattr("trading_bot.intelligence.requests.get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no")))
    assert get_recent_insider_filings() == []


def test_insider_parses_atom(monkeypatch):
    atom = """<?xml version="1.0"?>
<feed>
<entry>
<title>4 - Acme Corp (CIK 0000012345) (Reporting)</title>
<link href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000012345"/>
<updated>2026-04-25T12:00:00-04:00</updated>
<summary type="html">Form 4 filing — Accession Number: 0001234567-26-000001</summary>
</entry>
</feed>"""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.text = atom
    monkeypatch.setattr("trading_bot.intelligence.requests.get", lambda *a, **kw: fake_response)
    fil = get_recent_insider_filings(limit=5)
    assert len(fil) == 1
    assert "Acme Corp" in fil[0].company
    assert fil[0].cik == "0000012345"
    assert fil[0].accession == "0001234567-26-000001"
