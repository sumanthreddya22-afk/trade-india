"""Finnhub free-tier client. Soft-fail (returns empty / raises FinnhubUnavailable)
on errors so the rest of the bot can degrade gracefully."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import requests


_BASE = "https://finnhub.io/api/v1"
_TIMEOUT = 10
_USER_AGENT = "TradingBot/1.0 (paper-trading; bharath8887@gmail.com)"


class FinnhubUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class EarningsRow:
    symbol: str
    date: dt.date
    eps_estimate: float | None


@dataclass(frozen=True)
class CompanyProfile:
    symbol: str
    market_cap_musd: float | None
    ipo_date: dt.date | None
    exchange: str


class FinnhubClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._profile_cache: dict[str, CompanyProfile] = {}

    def _get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            return {}
        params = {**params, "token": self.api_key}
        try:
            r = requests.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT,
                             headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise FinnhubUnavailable(f"finnhub {path}: {e}") from e

    def earnings_calendar(self, start: dt.date, end: dt.date) -> list[EarningsRow]:
        body = self._get("/calendar/earnings",
                         {"from": start.isoformat(), "to": end.isoformat()})
        rows = body.get("earningsCalendar", []) if isinstance(body, dict) else []
        out: list[EarningsRow] = []
        for r in rows:
            try:
                out.append(EarningsRow(
                    symbol=r["symbol"],
                    date=dt.date.fromisoformat(r["date"]),
                    eps_estimate=r.get("epsEstimate"),
                ))
            except (KeyError, ValueError):
                continue
        return out

    def company_profile(self, symbol: str) -> CompanyProfile:
        if symbol in self._profile_cache:
            return self._profile_cache[symbol]
        body = self._get("/stock/profile2", {"symbol": symbol})
        ipo_str = body.get("ipo") if isinstance(body, dict) else None
        ipo_date: dt.date | None = None
        if ipo_str:
            try:
                ipo_date = dt.date.fromisoformat(ipo_str)
            except ValueError:
                pass
        prof = CompanyProfile(
            symbol=symbol,
            market_cap_musd=(body.get("marketCapitalization") if isinstance(body, dict) else None),
            ipo_date=ipo_date,
            exchange=(body.get("exchange") if isinstance(body, dict) else "") or "",
        )
        self._profile_cache[symbol] = prof
        return prof

    def has_earnings_in_window(self, symbol: str, start: dt.date, end: dt.date) -> bool:
        try:
            rows = self.earnings_calendar(start, end)
        except FinnhubUnavailable:
            return True  # conservative: treat as "earnings present" → block CSP
        return any(r.symbol == symbol for r in rows)
