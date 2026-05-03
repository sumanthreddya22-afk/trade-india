"""SEC CIK→ticker map downloader and lookup.

The SEC publishes a free, authoritative JSON mapping of every public
company CIK to its primary ticker(s). Used by the SEC 8-K collector to
attribute filings to specific symbols (the EDGAR feed is keyed by CIK,
not ticker).

  https://www.sec.gov/files/company_tickers.json

Format (top-level dict, integer-keyed by row index):
  {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}

Cached locally at ``data/sec_cik_map.json`` with a 7-day refresh. Stale
cache is still readable — only refresh on missing or week-old file. Network
failure during refresh falls back to the existing cache (fail-soft) — losing
new IPOs is far less bad than blocking the entire 8-K collector.

Required: SEC mandates a User-Agent string identifying the requester.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

import requests


log = logging.getLogger(__name__)

CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
CIK_MAP_PATH = Path("data/sec_cik_map.json")
REFRESH_DAYS = 7
HTTP_TIMEOUT = 30
# SEC requires a contactable User-Agent. Operator email per memory.
SEC_USER_AGENT = "TradingBot/1.0 (+bharath8887@gmail.com)"


def _is_stale(path: Path, *, max_age_days: int = REFRESH_DAYS) -> bool:
    if not path.exists():
        return True
    age_seconds = dt.datetime.now().timestamp() - path.stat().st_mtime
    return age_seconds > max_age_days * 86400


def refresh_cik_map(*, path: Path = CIK_MAP_PATH, force: bool = False) -> dict:
    """Download (or load cached) CIK map and return ticker→CIK dict.

    Returns ``{"AAPL": "0000320193", ...}`` — CIKs zero-padded to 10 digits
    as the EDGAR feed expects them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if force or _is_stale(path):
        try:
            r = requests.get(
                CIK_MAP_URL, timeout=HTTP_TIMEOUT,
                headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
            )
            r.raise_for_status()
            raw = r.json()
            path.write_text(json.dumps(raw))
        except Exception as e:  # noqa: BLE001
            log.warning("sec_cik_map refresh failed: %s — using stale cache if any", e)
            if not path.exists():
                return {}

    try:
        raw = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.error("sec_cik_map cache unreadable: %s", e)
        return {}

    return _build_ticker_to_cik(raw)


def _build_ticker_to_cik(raw: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for _, row in (raw or {}).items():
        if not isinstance(row, dict):
            continue
        ticker = (row.get("ticker") or "").upper().strip()
        cik = row.get("cik_str")
        if not ticker or cik is None:
            continue
        out[ticker] = str(int(cik)).zfill(10)
    return out


# Reverse lookup helper — useful when EDGAR returns a CIK and we need
# the ticker for attribution.
def build_cik_to_ticker(ticker_to_cik: dict[str, str]) -> dict[str, str]:
    return {cik: ticker for ticker, cik in ticker_to_cik.items()}


_cached_map: Optional[dict[str, str]] = None


def get_cik_for(ticker: str, *, refresh_if_missing: bool = False) -> Optional[str]:
    """Module-level convenience: cached lookup of one ticker."""
    global _cached_map
    if _cached_map is None:
        _cached_map = refresh_cik_map()
    cik = _cached_map.get(ticker.upper())
    if cik is None and refresh_if_missing:
        _cached_map = refresh_cik_map(force=True)
        cik = _cached_map.get(ticker.upper())
    return cik


def reset_cache() -> None:
    """Test hook — clear the module-level cache."""
    global _cached_map
    _cached_map = None
