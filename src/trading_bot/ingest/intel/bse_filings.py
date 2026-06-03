"""BSE/NSE corporate filings feed — replaces EDGAR for Indian markets.

Indian listed companies are required to file with both BSE and NSE.
Key filing types (SEBI Listing Obligations and Disclosure Requirements,
LODR Regulations 2015):
  - Quarterly results (within 45 days of quarter end)
  - Annual report
  - Board meeting notices + outcomes
  - Corporate actions (dividends, bonus, splits, rights)
  - Shareholding pattern (Q1–Q4)
  - Insider trading disclosures

Sources (all free, no auth):
  BSE Corporate Filings: https://www.bseindia.com/corporates/ann.html
  NSE Corporate Filings: https://www.nseindia.com/companies-listing/corporate-filings-announcements
  BSE API (XML/JSON):    https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
  NSE API:               https://www.nseindia.com/api/corporates-announcements?index=equities
  MCA21 (Ministry of Corporate Affairs): https://www.mca.gov.in/mcafoportal/viewCompanyMasterData.do
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from trading_bot.ingest.intel.base import (
    BaseIntelFeed, IntelRecord, IntelUnavailable,
)

log = logging.getLogger(__name__)

_BSE_ANNOUNCE_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?strCat=-1&strPrevDate={from_date}&strScrip=&strSearch=P"
    "&strToDate={to_date}&strType=C&subcategory=-1"
)

_NSE_ANNOUNCE_URL = (
    "https://www.nseindia.com/api/corporates-announcements"
    "?index=equities&from_date={from_date}&to_date={to_date}"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; trading-bot/0.1; research only)",
    "Accept": "application/json",
}


def _default_cache_path() -> Path:
    return Path.home() / ".cache" / "trading_bot" / "intel" / "bse_filings.json"


class BseFilingsFeed(BaseIntelFeed):
    """BSE/NSE corporate filing announcements feed.

    Used by the strategy_scout and hypothesis intake to detect earnings
    events, corporate actions, and material disclosures.
    """

    feed_id = "bse_filings"

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self.cache_path = cache_path or _default_cache_path()

    def refresh(self, *, asof: Optional[dt.date] = None) -> dict:
        asof = asof or dt.date.today()
        from_date = (asof - dt.timedelta(days=1)).strftime("%Y%m%d")
        to_date = asof.strftime("%Y%m%d")
        announcements: list[dict] = []
        try:
            url = _BSE_ANNOUNCE_URL.format(from_date=from_date, to_date=to_date)
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            for item in data.get("Table", []):
                announcements.append({
                    "symbol": item.get("SCRIP_CD", ""),
                    "company": item.get("SLONGNAME", ""),
                    "category": item.get("CATEGORYNAME", ""),
                    "headline": item.get("HEADLINE", ""),
                    "dt": item.get("NEWS_DT", ""),
                    "exchange": "BSE",
                })
        except Exception as e:  # noqa: BLE001
            log.warning("bse_filings: BSE fetch failed: %s", e)
        payload = {
            "published_iso": asof.isoformat(),
            "announcements": announcements,
            "fetched_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "count": len(announcements),
            "source_url": "https://api.bseindia.com/",
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=2))
        log.info("bse_filings refreshed: %d announcements", len(announcements))
        return payload

    def _load(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text())
        except json.JSONDecodeError:
            return {}

    def fetch(self, decision_date: dt.date) -> Mapping[str, IntelRecord]:
        cache = self._load()
        if not cache:
            raise IntelUnavailable("bse_filings cache empty")
        count = int(cache.get("count", 0))
        return {
            "bse_filing_count": IntelRecord(
                feed_id=self.feed_id,
                series_id="bse_filing_count",
                value=float(count),
                unit="count",
                source_ts=cache.get("published_iso", ""),
                fetched_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
                source_url="https://www.bseindia.com/corporates/ann.html",
            )
        }

    def query_features(
        self, symbol: str, asof: dt.datetime,
    ) -> Mapping[str, Any]:
        cache = self._load()
        announcements = cache.get("announcements", [])
        symbol_upper = symbol.upper()
        relevant = [
            a for a in announcements
            if a.get("symbol", "").upper() == symbol_upper
        ]
        return {
            "bse_announcement_count_today": len(relevant),
            "bse_has_earnings_today": any(
                "result" in a.get("category", "").lower()
                or "financial" in a.get("headline", "").lower()
                for a in relevant
            ),
        }


__all__ = ["BseFilingsFeed"]
