"""Intelligence aggregator — pulls free market data from multiple sources.

Sources (all free, no API keys required for current usage):
- Alpaca News API (per-symbol financial news)
- FRED (VIX, 10Y yield, fed funds — anonymous tier via fredgraph.csv)
- GDELT 2.0 (global news events with sentiment scoring)
- SEC EDGAR (Form 4 insider trades RSS feed)

Each fetch returns a normalized dataclass. Failures degrade gracefully —
the bot continues with partial data rather than blocking.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlencode

import requests
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from trading_bot.config import Settings


HTTP_TIMEOUT = 10  # seconds — generous for slow free APIs
USER_AGENT = "TradingBot/1.0 (paper-trading research; bharath8887@gmail.com)"


@dataclass(frozen=True)
class NewsItem:
    headline: str
    summary: str
    url: str
    published_at: datetime
    symbols: list[str]
    source: str


@dataclass(frozen=True)
class MacroSnapshot:
    vix: float | None
    yield_10y_pct: float | None
    fed_funds_pct: float | None
    fetched_at: datetime
    notes: str = ""


@dataclass(frozen=True)
class GdeltEvent:
    title: str
    url: str
    seendate: str
    sourcecountry: str
    sentiment: float  # -10 to +10 typical


@dataclass(frozen=True)
class InsiderFiling:
    company: str
    cik: str
    accession: str
    filed_at: str
    summary: str
    url: str


# --- Alpaca News (already covered by alpaca-py) -----------------------------

class AlpacaNews:
    def __init__(self, settings: Settings) -> None:
        self._client = NewsClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
        )

    def for_symbols(self, symbols: list[str], limit_per_symbol: int = 5) -> list[NewsItem]:
        if not symbols:
            return []
        items: list[NewsItem] = []
        # Strip slashes for crypto symbols (Alpaca news doesn't recognize 'BTC/USD')
        api_symbols = ",".join(s.replace("/", "") for s in symbols)
        try:
            req = NewsRequest(
                symbols=api_symbols,
                start=datetime.now(timezone.utc) - timedelta(days=2),
                limit=limit_per_symbol * max(1, len(symbols)),
            )
            resp = self._client.get_news(req)
        except Exception:
            return []
        # NewsSet has a .dict() method returning {'news': [...]}
        try:
            news_list = resp.dict().get("news", [])
        except Exception:
            news_list = []
        for n in news_list:
            try:
                created_at = n.get("created_at") or n.get("createdAt")
                if isinstance(created_at, str):
                    pub = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                elif isinstance(created_at, datetime):
                    pub = created_at
                else:
                    pub = datetime.now(timezone.utc)
                items.append(NewsItem(
                    headline=str(n.get("headline", "")),
                    summary=str(n.get("summary", "")),
                    url=str(n.get("url", "")),
                    published_at=pub,
                    symbols=list(n.get("symbols", [])),
                    source="alpaca",
                ))
            except Exception:
                continue
        return items


# --- FRED Macro -------------------------------------------------------------

# Use FRED's CSV download endpoint (anonymous, no key required)
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _fred_latest(series: str) -> float | None:
    try:
        r = requests.get(
            _FRED_CSV,
            params={"id": series},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        rows = [row for row in reader if row]
        # First row is header [DATE, SERIES_ID]; iterate from end for latest non-"."
        for row in reversed(rows[1:]):
            if len(row) >= 2 and row[1] not in {"", "."}:
                return float(row[1])
    except Exception:
        return None
    return None


def get_macro_snapshot() -> MacroSnapshot:
    vix = _fred_latest("VIXCLS")
    y10 = _fred_latest("DGS10")
    ffr = _fred_latest("DFF")
    return MacroSnapshot(
        vix=vix, yield_10y_pct=y10, fed_funds_pct=ffr,
        fetched_at=datetime.now(timezone.utc),
    )


# --- GDELT 2.0 doc API ------------------------------------------------------

_GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"


def get_gdelt_events(query: str = "stock market", max_records: int = 10) -> list[GdeltEvent]:
    try:
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(max_records),
            "sort": "DateDesc",
            "timespan": "24H",
        }
        r = requests.get(
            _GDELT_DOC,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    out: list[GdeltEvent] = []
    for art in data.get("articles", []):
        try:
            tone = art.get("tone")
            sentiment = float(tone) if tone is not None else 0.0
            out.append(GdeltEvent(
                title=str(art.get("title", "")),
                url=str(art.get("url", "")),
                seendate=str(art.get("seendate", "")),
                sourcecountry=str(art.get("sourcecountry", "")),
                sentiment=sentiment,
            ))
        except Exception:
            continue
    return out


# --- SEC EDGAR Form 4 RSS ---------------------------------------------------

_EDGAR_LATEST = "https://www.sec.gov/cgi-bin/browse-edgar"


def get_recent_insider_filings(limit: int = 20) -> list[InsiderFiling]:
    """Get recent Form 4 (insider trade) filings from EDGAR."""
    try:
        params = {
            "action": "getcurrent",
            "type": "4",
            "company": "",
            "dateb": "",
            "owner": "include",
            "count": str(limit),
            "output": "atom",
        }
        r = requests.get(
            _EDGAR_LATEST,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        text = r.text
    except Exception:
        return []
    out: list[InsiderFiling] = []
    # Lightweight Atom parser — avoids xml dependency for a known shape
    import re
    entries = re.findall(r"<entry>(.+?)</entry>", text, re.DOTALL)
    for e in entries[:limit]:
        try:
            title = re.search(r"<title>(.+?)</title>", e, re.DOTALL)
            link_match = re.search(r'<link.+?href="(.+?)"', e)
            updated = re.search(r"<updated>(.+?)</updated>", e)
            summary = re.search(r"<summary[^>]*>(.+?)</summary>", e, re.DOTALL)
            company_text = title.group(1).strip() if title else ""
            cik_match = re.search(r"\(CIK (\d+)\)", company_text)
            accession_match = re.search(r"Accession Number: ([\d-]+)", e)
            out.append(InsiderFiling(
                company=company_text,
                cik=cik_match.group(1) if cik_match else "",
                accession=accession_match.group(1) if accession_match else "",
                filed_at=updated.group(1).strip() if updated else "",
                summary=summary.group(1).strip()[:500] if summary else "",
                url=link_match.group(1) if link_match else "",
            ))
        except Exception:
            continue
    return out


# --- Composite ---------------------------------------------------------------

@dataclass(frozen=True)
class IntelligenceBundle:
    macro: MacroSnapshot
    news_by_symbol: dict[str, list[NewsItem]]
    gdelt: list[GdeltEvent]
    insider: list[InsiderFiling]
    fetched_at: datetime


class IntelligenceAggregator:
    def __init__(self, settings: Settings) -> None:
        self._news = AlpacaNews(settings)

    def gather(self, symbols: list[str]) -> IntelligenceBundle:
        macro = get_macro_snapshot()
        news_items = self._news.for_symbols(symbols, limit_per_symbol=3)
        news_by_symbol: dict[str, list[NewsItem]] = {s: [] for s in symbols}
        for item in news_items:
            for s in item.symbols:
                if s in news_by_symbol and len(news_by_symbol[s]) < 3:
                    news_by_symbol[s].append(item)
        gdelt = get_gdelt_events(query="stock market OR S&P 500 OR Federal Reserve", max_records=8)
        insider = get_recent_insider_filings(limit=10)
        return IntelligenceBundle(
            macro=macro,
            news_by_symbol=news_by_symbol,
            gdelt=gdelt,
            insider=insider,
            fetched_at=datetime.now(timezone.utc),
        )
