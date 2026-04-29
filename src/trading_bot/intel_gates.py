"""Per-trade news/intel gate functions.

Each gate is a free function that returns:
  * a string skip-reason → orchestrator blocks the entry
  * None → gate passes (or the source is unavailable; we never block on
    intel failure, only on confirmed-bad intel)

Gates are filter-only: they can never *open* a new position, only refuse one.
That makes adding a gate strictly safer than not adding it (worst case: a
gate falsely blocks a winning trade; never opens a losing one).

Caching strategy per gate (in-process, scan-bounded):
  * Stock earnings: 1 day TTL, per-symbol
  * Stock insider cluster: 24h TTL, per-symbol
  * Crypto fear & greed: 1h TTL (index updates daily)
  * Crypto Reddit mentions: 5min TTL (one shared snapshot per scan)
  * Crypto CoinGecko sentiment: 30min TTL, per-symbol
  * GDELT macro shock: 30min TTL (one shared score per scan)
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from dataclasses import dataclass

import requests

from trading_bot.config import Settings
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient, FinnhubUnavailable


log = logging.getLogger(__name__)
_TIMEOUT = 10
_USER_AGENT = "TradingBot/1.0 (paper-trading; bharath8887@gmail.com)"

# ============================================================================
# Lazy-init module-level singletons
# ============================================================================

_finnhub_lock = threading.Lock()
_finnhub: FinnhubClient | None = None


def _get_finnhub() -> FinnhubClient:
    global _finnhub
    if _finnhub is None:
        with _finnhub_lock:
            if _finnhub is None:
                key = os.environ.get("FINNHUB_API_KEY", "")
                _finnhub = FinnhubClient(api_key=key)
    return _finnhub


_apewisdom_lock = threading.Lock()
_apewisdom: ApeWisdomClient | None = None


def _get_apewisdom() -> ApeWisdomClient:
    global _apewisdom
    if _apewisdom is None:
        with _apewisdom_lock:
            if _apewisdom is None:
                _apewisdom = ApeWisdomClient()
    return _apewisdom


# ============================================================================
# In-process TTL caches
# ============================================================================

@dataclass
class _TTLEntry:
    value: object
    expires_at: dt.datetime


_cache: dict[str, _TTLEntry] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        e = _cache.get(key)
        if e is None:
            return None
        if dt.datetime.now(dt.timezone.utc) >= e.expires_at:
            del _cache[key]
            return None
        return e.value


def _cache_put(key: str, value, ttl_seconds: int) -> None:
    with _cache_lock:
        _cache[key] = _TTLEntry(
            value=value,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_seconds),
        )


# ============================================================================
# Tier 1.1 — Stock earnings gate (Finnhub)
# ============================================================================

def stock_earnings_gate(symbol: str, *, lookahead_days: int = 5) -> str | None:
    """Block stock entries when the symbol has earnings within the next
    `lookahead_days` calendar days. Avoids buying just before a binary
    gap-risk event.

    Caches per-symbol per-day. Finnhub failure → returns None (don't block)."""
    today = dt.date.today()
    key = f"earn:{symbol}:{today.isoformat()}:{lookahead_days}"
    cached = _cache_get(key)
    if cached is not None:
        return cached if cached else None

    fin = _get_finnhub()
    if not fin.api_key:
        return None  # no key → silently pass
    end = today + dt.timedelta(days=lookahead_days)
    try:
        rows = fin.earnings_calendar(today, end)
    except FinnhubUnavailable:
        return None  # API down → don't block
    has_earnings = any(r.symbol == symbol for r in rows)
    reason = (f"earnings within {lookahead_days}d (Finnhub)"
              if has_earnings else "")
    _cache_put(key, reason, ttl_seconds=24 * 3600)
    return reason if has_earnings else None


# ============================================================================
# Tier 2.2 — Stock insider-cluster gate (Finnhub /stock/insider-transactions)
# ============================================================================

def stock_insider_cluster_gate(
    symbol: str, *, lookback_days: int = 90,
    sell_volume_threshold: int = 5,
) -> str | None:
    """Block stock entries when there's a SIGNIFICANT cluster of insider
    SELLING in the last `lookback_days`. Insider selling clusters often
    precede negative news. Insider BUYING is permissive — we don't block,
    just note as a positive signal in logs.

    Threshold: ≥ `sell_volume_threshold` distinct insider sell transactions
    in the lookback window. Finnhub failure → returns None.

    Note: insider data IS lagging (filings are 2 days late by SEC rule).
    Use as a coarse "smell test" not a precise signal."""
    key = f"insider:{symbol}:{dt.date.today().isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached if cached else None

    fin = _get_finnhub()
    if not fin.api_key:
        return None
    end = dt.date.today()
    start = end - dt.timedelta(days=lookback_days)
    try:
        body = fin._get(  # noqa: SLF001 — internal helper, accepted
            "/stock/insider-transactions",
            {"symbol": symbol, "from": start.isoformat(), "to": end.isoformat()},
        )
    except Exception:
        return None
    rows = body.get("data", []) if isinstance(body, dict) else []
    sells = [r for r in rows if (r.get("transactionCode", "") or "").upper().startswith("S")]
    reason = ""
    if len(sells) >= sell_volume_threshold:
        reason = (f"insider sell cluster ({len(sells)} sells in {lookback_days}d)")
    _cache_put(key, reason, ttl_seconds=24 * 3600)
    return reason if reason else None


# ============================================================================
# Tier 1.2 — Crypto Fear & Greed gate (Alternative.me, free, no auth)
# ============================================================================

_FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"


def _fetch_fear_greed() -> int | None:
    """Returns current Crypto Fear & Greed index (0-100). None on failure."""
    cached = _cache_get("fng")
    if cached is not None:
        return cached
    try:
        r = requests.get(_FEAR_GREED_URL, timeout=_TIMEOUT,
                         headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        body = r.json()
        rows = (body or {}).get("data") or []
        if not rows:
            return None
        score = int(rows[0].get("value") or 0)
    except Exception as e:
        log.info("fear_greed fetch failed: %s", e)
        return None
    _cache_put("fng", score, ttl_seconds=3600)
    return score


def crypto_fear_greed_gate(*, floor: int = 20, ceiling: int = 80) -> str | None:
    """Block crypto entries when the Fear & Greed index is outside [floor,
    ceiling]. Extreme fear (<20) = capitulation likely → wait. Extreme
    greed (>80) = euphoria → over-positioning risk. Both are entry-blockers.

    Source unavailability → returns None."""
    score = _fetch_fear_greed()
    if score is None:
        return None
    if score < floor:
        return f"fear_greed extreme fear ({score} < {floor})"
    if score > ceiling:
        return f"fear_greed extreme greed ({score} > {ceiling})"
    return None


# ============================================================================
# Tier 1.3 — Crypto Reddit mention spike (ApeWisdom r/CryptoCurrency)
# ============================================================================

_APEWISDOM_CRYPTO_URL = (
    "https://apewisdom.io/api/v1.0/filter/cryptocurrencies"
)


def _fetch_crypto_mentions() -> dict[str, dict]:
    """Returns {coin_symbol: {mentions, mentions_24h_ago, rank}} from
    ApeWisdom's r/CryptoCurrency aggregator. Empty dict on failure."""
    cached = _cache_get("apewisdom_crypto")
    if cached is not None:
        return cached
    try:
        r = requests.get(_APEWISDOM_CRYPTO_URL, timeout=_TIMEOUT,
                         headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        body = r.json()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for row in (body.get("results") or []):
        try:
            ticker = str(row.get("ticker") or "").upper()
            if ticker:
                out[ticker] = {
                    "mentions": int(row.get("mentions") or 0),
                    "mentions_24h_ago": int(row.get("mentions_24h_ago") or 0),
                    "rank": int(row.get("rank") or 999),
                }
        except (TypeError, ValueError):
            continue
    _cache_put("apewisdom_crypto", out, ttl_seconds=300)
    return out


def crypto_reddit_spike_gate(symbol: str, *, multiplier: float = 2.0) -> str | None:
    """Block crypto entries when r/CryptoCurrency mentions for the underlying
    coin are spiking ≥ multiplier × 24h-ago. Social spikes on crypto often
    precede pump-dump cycles."""
    # Map BTC/USD → BTC, ETH/USD → ETH, etc.
    base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
    snap = _fetch_crypto_mentions()
    row = snap.get(base)
    if row is None:
        return None
    if row["mentions_24h_ago"] <= 0:
        return None
    if row["mentions"] >= row["mentions_24h_ago"] * multiplier:
        return (f"reddit_spike {base} {row['mentions']} mentions "
                f"vs {row['mentions_24h_ago']} 24h ago")
    return None


# ============================================================================
# Tier 2.1 — Macro-shock gate (GDELT)
# ============================================================================

def _fetch_gdelt_score() -> float | None:
    """Returns aggregate sentiment score from GDELT for last 24h on broad
    market topic queries ("stock market OR Fed OR S&P 500"). Range
    roughly -10..+10 (more negative = more negative aggregate news). None
    on failure.

    Cached 30min — GDELT updates every 15 min anyway."""
    from trading_bot.intelligence import get_gdelt_events
    cached = _cache_get("gdelt")
    if cached is not None:
        return cached
    try:
        events = get_gdelt_events(
            query="stock market OR S&P 500 OR Federal Reserve",
            max_records=8,
        )
    except Exception:
        return None
    if not events:
        return None
    avg = sum(e.sentiment for e in events) / len(events)
    _cache_put("gdelt", avg, ttl_seconds=1800)
    return avg


def macro_shock_gate(*, threshold: float = -3.0) -> str | None:
    """Block ALL entries when aggregate macro sentiment in last 24h is
    extremely negative. Threshold is in GDELT's sentiment scale (-10..+10);
    -3.0 = persistent very-bad-news regime. Source unavailability → None."""
    score = _fetch_gdelt_score()
    if score is None:
        return None
    if score <= threshold:
        return f"macro_shock GDELT {score:.2f} ≤ {threshold}"
    return None


# ============================================================================
# Tier 2.3 — CoinGecko per-coin community sentiment
# ============================================================================

# Minimal symbol→id map for the most-likely tickers in our crypto universe.
# CoinGecko ids are slugs not tickers. Off by default in config; needs this
# map populated for any symbol you want to gate on. Operator can extend.
_COINGECKO_ID_MAP: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "ADA": "cardano", "XRP": "ripple", "DOGE": "dogecoin",
    "AVAX": "avalanche-2", "LINK": "chainlink", "DOT": "polkadot",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "UNI": "uniswap",
    "ARB": "arbitrum", "FIL": "filecoin", "AAVE": "aave",
    "CRV": "curve-dao-token", "GRT": "the-graph", "MATIC": "matic-network",
    "POL": "matic-network", "ATOM": "cosmos", "NEAR": "near",
}

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def _fetch_coingecko_sentiment(coin_id: str) -> float | None:
    """Returns coin's `sentiment_votes_up_percentage` (0..100). None on failure.
    Cached per-coin 30min."""
    key = f"cg:{coin_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            f"{_COINGECKO_BASE}/coins/{coin_id}",
            params={"localization": "false", "tickers": "false",
                    "market_data": "false", "community_data": "false",
                    "developer_data": "false", "sparkline": "false"},
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        body = r.json()
        score = float(body.get("sentiment_votes_up_percentage") or 0.0)
    except Exception:
        return None
    _cache_put(key, score, ttl_seconds=1800)
    return score


def crypto_coingecko_gate(symbol: str, *, sentiment_floor: float = 50.0) -> str | None:
    """Block crypto entries when CoinGecko's community-sentiment up-vote
    percentage is below floor. Source unavailability → None.
    Symbol must be in _COINGECKO_ID_MAP (extend that dict to widen coverage)."""
    base = symbol.split("/")[0].upper() if "/" in symbol else symbol.upper()
    coin_id = _COINGECKO_ID_MAP.get(base)
    if coin_id is None:
        return None  # unmapped — don't block
    score = _fetch_coingecko_sentiment(coin_id)
    if score is None:
        return None
    if score < sentiment_floor:
        return f"coingecko sentiment {score:.0f}% < {sentiment_floor:.0f}%"
    return None


def _reset_caches_for_tests() -> None:
    """Test helper: wipe all module-level caches between test cases."""
    with _cache_lock:
        _cache.clear()
