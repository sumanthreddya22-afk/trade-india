"""Massive (Polygon) REST client.

Centralised adapter for the three Massive endpoints we use in production
(beyond the historical bar fetches that already live in
backtest/massive_bar_loader.py):

1. `daily_grouped(date)` — one call returns OHLC + volume for every US
   equity that traded on `date`. Replaces the per-ticker `get_active_assets
   + bar_loader` loop in the screener; turns ~3000 calls into 1.
2. `news(ticker, since)` — per-ticker articles with built-in sentiment.
3. `short_interest(ticker)` — biweekly FINRA short interest series.

Reads `POLYGON_API_KEY` from settings (or env). Fails fast with a clear
error if the key is missing — better than a 401 deep in the stack.

Rate-limit handling: per-minute quotas exist on the user's plan
(~5 calls/min). The client enforces MIN_CALL_INTERVAL_S between
calls on a single instance and applies an exponential BACKOFF_SCHEDULE
on 429 responses before raising MassiveRateLimitError.
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import requests

from trading_bot.config import Settings

POLYGON_BASE = "https://api.polygon.io"
HTTP_TIMEOUT = 30
# Polygon free/starter plan is ~5 calls/min. 12s is the floor; 13 buffers.
MIN_CALL_INTERVAL_S = 13.0
# On 429: sleep for the next value, retry, then advance. Last entry is
# ~5 minutes; total worst-case wait across the schedule is ~9 minutes.
BACKOFF_SCHEDULE = (10, 30, 60, 120, 300)


class MassiveAuthError(RuntimeError):
    pass


class MassiveRateLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GroupedBar:
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None
    trade_date: date


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    publisher: str
    title: str
    url: str
    published_utc: datetime
    tickers: tuple[str, ...]
    description: str
    # Per-ticker sentiment from Polygon's `insights`. Empty if not provided.
    sentiments: dict[str, str]  # ticker -> "positive" | "neutral" | "negative"
    sentiment_reasons: dict[str, str]


@dataclass(frozen=True)
class ShortInterestRecord:
    settlement_date: date
    short_interest: int
    avg_daily_volume: int
    days_to_cover: float


# ----------------------------------------------------------------------


class MassiveClient:
    def __init__(self, api_key: str | None = None) -> None:
        if api_key is None:
            try:
                api_key = Settings().polygon_api_key or ""
            except Exception:
                api_key = ""
        if not api_key:
            raise MassiveAuthError(
                "POLYGON_API_KEY not set. Add it to .env (POLYGON_API_KEY=...) "
                "or pass api_key explicitly."
            )
        self._api_key = api_key

    # ---- HTTP plumbing ----

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        # Per-instance throttle: enforce MIN_CALL_INTERVAL_S between calls.
        now = time.monotonic()
        last = getattr(self, "_last_call_at", None)
        if last is not None:
            elapsed = now - last
            if elapsed < MIN_CALL_INTERVAL_S:
                time.sleep(MIN_CALL_INTERVAL_S - elapsed)

        url = f"{POLYGON_BASE}{path}"
        full_params = dict(params or {})
        full_params["apiKey"] = self._api_key

        for backoff in BACKOFF_SCHEDULE:
            r = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
            self._last_call_at = time.monotonic()
            if r.status_code == 429:
                time.sleep(backoff)
                continue
            if r.status_code in (401, 403):
                raise MassiveAuthError(
                    f"Massive auth/entitlement error on {path}: {r.status_code} {r.text}"
                )
            r.raise_for_status()
            return r

        # One last attempt after exhausting backoff
        r = requests.get(url, params=full_params, timeout=HTTP_TIMEOUT)
        self._last_call_at = time.monotonic()
        if r.status_code == 429:
            raise MassiveRateLimitError(
                f"rate-limited {len(BACKOFF_SCHEDULE) + 1}x on {path}; giving up"
            )
        if r.status_code in (401, 403):
            raise MassiveAuthError(
                f"Massive auth/entitlement error on {path}: {r.status_code} {r.text}"
            )
        r.raise_for_status()
        return r

    # ---- daily grouped aggregates ----

    def grouped_recent_days(self, days: int = 7) -> dict[str, pd.DataFrame]:
        """Pull the last `days` trading days of grouped OHLC and return a
        per-ticker DataFrame (one DF per symbol with the columns the
        stage-1 screener expects: open/high/low/close/volume).

        Walks back day-by-day until enough non-empty days are found OR
        14 calendar days have been tried. Skips holidays/weekends naturally.
        """
        from datetime import timedelta as _td

        per_day: list[tuple[date, pd.DataFrame]] = []
        cur = datetime.now(timezone.utc).date()
        tries = 0
        while len(per_day) < days and tries < days + 7:
            cur -= _td(days=1)
            tries += 1
            df = self.daily_grouped(cur)
            if not df.empty:
                per_day.append((cur, df))

        if not per_day:
            return {}

        # Build per-ticker frames from the per-day grouped data.
        per_day.sort(key=lambda t: t[0])  # ascending by date
        tickers = set()
        for _, df in per_day:
            tickers.update(df.index)

        out: dict[str, pd.DataFrame] = {}
        for tkr in tickers:
            rows = []
            idx = []
            for d, df in per_day:
                if tkr in df.index:
                    r = df.loc[tkr]
                    rows.append({
                        "open": float(r["o"]), "high": float(r["h"]),
                        "low": float(r["l"]), "close": float(r["c"]),
                        "volume": float(r["v"]),
                    })
                    idx.append(pd.Timestamp(d))
            if rows:
                out[tkr] = pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="timestamp"))
        return out

    def daily_grouped(self, on: date, *, adjusted: bool = True) -> pd.DataFrame:
        """All US equity OHLC for a single trading date in one call.

        Returns DataFrame indexed by ticker with columns o, h, l, c, v, vw.
        Empty DataFrame if `on` is a non-trading day (Polygon returns no rows).
        """
        path = f"/v2/aggs/grouped/locale/us/market/stocks/{on.isoformat()}"
        r = self._get(path, params={"adjusted": "true" if adjusted else "false"})
        # Polygon returns JSON for this endpoint
        try:
            data = r.json()
        except ValueError:
            return pd.DataFrame()
        results = data.get("results") or []
        if not results:
            return pd.DataFrame()
        rows = [
            {
                "ticker": str(b["T"]),
                "o": float(b["o"]), "h": float(b["h"]),
                "l": float(b["l"]), "c": float(b["c"]),
                "v": float(b["v"]),
                "vw": float(b.get("vw") or 0.0),
            }
            for b in results
            if b.get("T") and b.get("c") is not None
        ]
        df = pd.DataFrame(rows).set_index("ticker")
        return df

    # ---- news + sentiment ----

    def news(
        self,
        ticker: str,
        *,
        published_utc_gte: str | None = None,
        limit: int = 50,
    ) -> list[NewsArticle]:
        """Per-ticker news. `published_utc_gte` is an ISO date or RFC-3339 ts."""
        params: dict[str, Any] = {"ticker": ticker, "limit": limit, "order": "desc"}
        if published_utc_gte:
            params["published_utc.gte"] = published_utc_gte
        r = self._get("/v2/reference/news", params=params)
        # Polygon news comes back as JSON by default (despite earlier CSV
        # observation; the response varies by Accept header).
        try:
            data = r.json()
        except ValueError:
            return []
        results = data.get("results") or []
        out: list[NewsArticle] = []
        for art in results:
            try:
                pub = datetime.fromisoformat(
                    str(art.get("published_utc", "")).replace("Z", "+00:00")
                )
            except Exception:
                continue
            insights = art.get("insights") or []
            sentiments: dict[str, str] = {}
            reasons: dict[str, str] = {}
            for ins in insights:
                t = ins.get("ticker")
                if t:
                    sentiments[t] = str(ins.get("sentiment", "neutral"))
                    reasons[t] = str(ins.get("sentiment_reasoning", ""))
            out.append(NewsArticle(
                article_id=str(art.get("id", "")),
                publisher=str(art.get("publisher", {}).get("name", "")),
                title=str(art.get("title", "")),
                url=str(art.get("article_url", "")),
                published_utc=pub,
                tickers=tuple(art.get("tickers") or []),
                description=str(art.get("description", "")),
                sentiments=sentiments,
                sentiment_reasons=reasons,
            ))
        return out

    def aggregate_sentiment(
        self, ticker: str, *, lookback_days: int = 3
    ) -> tuple[float, int, str]:
        """Score a ticker's recent news sentiment.

        Returns (score, n_articles, dominant_label).
        Score: average across articles where the ticker has explicit sentiment.
        +1 = positive, 0 = neutral, -1 = negative.
        n_articles: number of articles factored into the score.
        dominant_label: the most-common sentiment word, or "no-data".
        """
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
        articles = self.news(ticker, published_utc_gte=since, limit=50)

        scored: list[int] = []
        labels: list[str] = []
        for art in articles:
            label = art.sentiments.get(ticker)
            if label is None:
                continue
            labels.append(label)
            if label == "positive":
                scored.append(1)
            elif label == "negative":
                scored.append(-1)
            else:
                scored.append(0)

        if not scored:
            return 0.0, 0, "no-data"
        avg = sum(scored) / len(scored)
        # Dominant label
        from collections import Counter
        dominant = Counter(labels).most_common(1)[0][0]
        return avg, len(scored), dominant

    # ---- short interest ----

    def short_interest(
        self, ticker: str, *, limit: int = 26
    ) -> list[ShortInterestRecord]:
        """Latest `limit` biweekly short-interest snapshots for `ticker`,
        most-recent first."""
        r = self._get("/stocks/v1/short-interest", params={
            "ticker": ticker, "limit": limit, "order": "desc",
        })
        # Polygon returns CSV here — see /tmp ratings of earlier call.
        text = r.text
        out: list[ShortInterestRecord] = []
        # Quick parse — first line is header
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                out.append(ShortInterestRecord(
                    settlement_date=date.fromisoformat(row["settlement_date"]),
                    short_interest=int(row["short_interest"]),
                    avg_daily_volume=int(row["avg_daily_volume"]),
                    days_to_cover=float(row["days_to_cover"]),
                ))
            except Exception:
                continue
        return out
