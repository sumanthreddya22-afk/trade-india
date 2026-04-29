"""ApeWisdom WSB / r/stocks mention tracker. No auth, soft-fail to empty dict
so the bot keeps running when the source is down."""
from __future__ import annotations

from dataclasses import dataclass

import requests

_BASE = "https://apewisdom.io/api/v1.0/filter/wallstreetbets"
_TIMEOUT = 10
_USER_AGENT = "TradingBot/1.0 (paper-trading; bharath8887@gmail.com)"


@dataclass(frozen=True)
class MentionRow:
    ticker: str
    rank: int
    mentions: int
    mentions_24h_ago: int


class ApeWisdomClient:
    def __init__(self) -> None:
        self._last: dict[str, MentionRow] = {}

    def wallstreetbets_mentions(self) -> dict[str, MentionRow]:
        try:
            r = requests.get(_BASE, timeout=_TIMEOUT,
                             headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            body = r.json()
        except Exception:
            return {}
        out: dict[str, MentionRow] = {}
        for row in (body.get("results") or []):
            try:
                out[row["ticker"]] = MentionRow(
                    ticker=row["ticker"], rank=int(row.get("rank") or 999),
                    mentions=int(row.get("mentions") or 0),
                    mentions_24h_ago=int(row.get("mentions_24h_ago") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._last = out
        return out

    def is_spike(self, symbol: str, *, multiplier: float) -> bool:
        row = self._last.get(symbol)
        if not row or row.mentions_24h_ago <= 0:
            return False
        return row.mentions >= row.mentions_24h_ago * multiplier
