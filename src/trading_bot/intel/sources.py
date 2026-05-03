"""Source collectors — pull from existing intel modules and write events.

Each ``collect_*`` function:
  1. Calls into the pre-existing module that already does the HTTP work
     (intelligence.py, intelligence_apewisdom.py, vip_tweets.py).
  2. Normalizes the response into ``write_event`` calls.
  3. Tags the source string consistently with ``aggregator.SOURCE_WEIGHTS``.

Failure mode: every collector traps its own exceptions and returns the
count of events written. A dead source contributes zero, never crashes
the role. The role logs the per-source counts so an outage is visible
on the dashboard within one tick.

The collectors do NOT do entity extraction — they rely on the
underlying modules already returning per-symbol items (Alpaca News,
ApeWisdom, Finnhub all do). For sources that return symbol-less
narratives (GDELT raw events, VIP tweets), we run the existing
``vip_tweets.extract_tickers`` style heuristics, which the bot already
has elsewhere. Future enhancement: route to Claude via mailbox for
ambiguous cases (subscription-billed).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

from trading_bot.intel.aggregator import write_event


log = logging.getLogger(__name__)


def collect_alpaca_news(
    engine, *, settings, symbols: Iterable[str], limit_per_symbol: int = 5
) -> dict:
    """Walk Alpaca News for the input symbols, write per-symbol events.

    ``symbols`` is the seed list — we don't need the full universe; even
    a wide seed (CORE_LIQUID_TICKERS) is fine since Alpaca News indexes
    by-symbol and returns nothing for symbols with no recent news.
    """
    from trading_bot.intelligence import AlpacaNews
    written = 0
    skipped = 0
    try:
        client = AlpacaNews(settings)
        items = client.for_symbols(list(symbols), limit_per_symbol=limit_per_symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("alpaca_news collect failed: %s", e)
        return {"source": "alpaca_news", "written": 0, "skipped": 0, "error": str(e)}

    for it in items:
        for sym in (it.symbols or []):
            ok = write_event(
                engine,
                symbol=sym,
                asset_class=_guess_asset_class(sym),
                source="alpaca_news",
                headline=it.headline,
                url=it.url,
                event_at=getattr(it, "published_at", None),
            )
            if ok:
                written += 1
            else:
                skipped += 1
    return {"source": "alpaca_news", "written": written, "skipped": skipped}


def collect_gdelt(engine, *, max_records: int = 50) -> dict:
    """GDELT is a global news firehose without per-symbol tagging. We
    do a coarse heuristic: extract uppercase 1-5 char tokens that look
    like tickers. Most GDELT records won't contain a ticker; the dedup
    index makes re-runs cheap.

    Skipped by default: GDELT signal density per query is low. Better
    to run it as a "filter for headline events" — disabled until the
    role enables it. Kept as a stub for the next phase.
    """
    return {"source": "gdelt", "written": 0, "skipped": 0, "note": "stub"}


def collect_sec_form4(engine, *, limit: int = 20) -> dict:
    """Legacy stub kept as a no-op. Phase A wired SEC 8-K instead — Form-4
    insider trading is a follow-up source. Returns zero-written; never raises.
    """
    return {"source": "sec_form4", "written": 0, "skipped": 0, "note": "cik_map_not_wired"}


# ---------------------------------------------------------------------------
# Phase A — diversified stock-news sources (defense-in-depth)
# ---------------------------------------------------------------------------
# Pre-Phase-A, the only stock-news writer was Alpaca News — single-vendor
# dependency. Phase A adds: Polygon News (native sentiment), SEC 8-K (legal
# filings, highest-trust), Yahoo / GoogleNews RSS (broad publisher coverage),
# Reddit r/stocks etc (broader than ApeWisdom's WSB-only), NewsAPI (80k+
# source aggregator). All collectors are best-effort: outages return
# zero-written, never raise.
# ---------------------------------------------------------------------------

# Sentiment scalars per Polygon's per-ticker label. Polygon returns
# "positive" / "neutral" / "negative" — we map to floats for write_event.
_POLYGON_SENTIMENT_FLOAT = {
    "positive": 0.6,
    "neutral":  0.0,
    "negative": -0.6,
}

# 8-K item-type → default sentiment scalar. Item 2.06 (Material Impairment)
# and 4.02 (Non-Reliance on Prior Financials) are unambiguously bad. Item
# 2.02 (Earnings) is content-driven so we stay neutral. Item 7.01 (Reg FD)
# is informational. Anything not listed defaults to neutral — we don't
# guess. Source: SEC 8-K item index.
_SEC_8K_ITEM_SENTIMENT = {
    "1.01":  0.0,   # Material Definitive Agreement
    "1.02": -0.4,   # Termination of Material Agreement
    "2.01":  0.0,   # Completion of Acquisition / Disposition
    "2.02":  0.0,   # Earnings Release (content-driven)
    "2.04": -0.5,   # Triggering Events Accelerating Obligations
    "2.05": -0.6,   # Costs Associated with Exit / Disposal Activities
    "2.06": -0.8,   # Material Impairment
    "3.01": -0.7,   # Notice of Delisting / Failure to Satisfy Listing
    "4.01": -0.4,   # Changes in Registrant's Certifying Accountant
    "4.02": -0.8,   # Non-Reliance on Previously Issued Financial Statements
    "5.02":  0.0,   # Director / Officer Departure / Election
    "5.03":  0.0,   # Amendments to Articles / Bylaws
    "7.01":  0.0,   # Reg FD Disclosure
    "8.01":  0.0,   # Other Events
}

_PHASE_A_USER_AGENT = "TradingBot/1.0 (+bharath8887@gmail.com)"
_PHASE_A_HTTP_TIMEOUT = 15


def _elevated_symbols(
    engine, *, min_score: float = 2.0, asset_class: str = "stock", limit: int = 60,
) -> list[str]:
    """Return tickers already trending in intel_candidates for asset_class.
    Used by elevated-only collectors (Polygon News, Yahoo RSS, GoogleNews RSS,
    Reddit) to bound rate-limited API calls. Empty list = no elevation yet
    (cold start) — caller should fall back to a static seed.
    """
    from sqlalchemy.orm import Session
    from sqlalchemy import desc as _desc
    from trading_bot.state_db import IntelCandidate
    try:
        with Session(engine) as session:
            rows = (
                session.query(IntelCandidate.symbol)
                .filter(IntelCandidate.asset_class == asset_class)
                .filter(IntelCandidate.score >= min_score)
                .order_by(_desc(IntelCandidate.score))
                .limit(limit)
                .all()
            )
        return [r[0] for r in rows]
    except Exception as e:  # noqa: BLE001
        log.warning("_elevated_symbols query failed: %s", e)
        return []


def collect_massive_news(
    engine, *, settings, symbols: Iterable[str], limit_per_symbol: int = 10,
) -> dict:
    """Polygon (Massive) News with native per-ticker sentiment.

    Calls ``MassiveClient.news()`` per symbol. Polygon free/starter is
    ~5 calls/min; the client enforces 13s between calls. Caller should pass
    ``symbols`` already gated to elevated names (typically <30/tick) to stay
    inside the rate budget.

    Empty ``polygon_api_key`` → silent skip. Auth/rate errors return per-
    source error counts without raising.
    """
    syms = list(symbols)
    if not syms:
        return {"source": "polygon_news", "written": 0, "skipped": 0, "note": "no symbols"}

    from trading_bot.massive_client import (
        MassiveAuthError, MassiveClient, MassiveRateLimitError,
    )
    api_key = getattr(settings, "polygon_api_key", "") or ""
    if not api_key:
        return {"source": "polygon_news", "written": 0, "skipped": 0, "note": "no api key"}

    try:
        client = MassiveClient(api_key=api_key)
    except MassiveAuthError as e:
        return {"source": "polygon_news", "written": 0, "skipped": 0, "error": str(e)}

    written = 0
    skipped = 0
    errors = 0
    since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    ).date().isoformat()
    for sym in syms:
        try:
            articles = client.news(
                sym, published_utc_gte=since, limit=limit_per_symbol,
            )
        except MassiveRateLimitError:
            errors += 1
            log.warning("polygon_news rate-limited on %s; stopping batch", sym)
            break
        except MassiveAuthError:
            errors += 1
            log.warning("polygon_news auth error on %s; stopping batch", sym)
            break
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("polygon_news fetch failed for %s: %s", sym, e)
            continue
        for art in articles:
            label = (art.sentiments.get(sym) or "").lower()
            sentiment = _POLYGON_SENTIMENT_FLOAT.get(label)
            ok = write_event(
                engine,
                symbol=sym.upper(),
                asset_class=_guess_asset_class(sym),
                source="polygon_news",
                headline=art.title,
                url=art.url,
                sentiment=sentiment,
                event_at=art.published_utc,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    out = {"source": "polygon_news", "written": written, "skipped": skipped}
    if errors:
        out["errors"] = errors
    return out


def _parse_8k_items(text: str) -> list[str]:
    """Extract '2.02', '5.02' style item codes from an EDGAR atom title /
    summary. Filings list them as 'Item 2.02 ...' or 'Items 2.02, 7.01'.
    """
    import re
    pattern = re.compile(r"\bItems?\s+([0-9]+\.[0-9]+(?:\s*,\s*[0-9]+\.[0-9]+)*)", re.IGNORECASE)
    out: list[str] = []
    for m in pattern.finditer(text or ""):
        chunk = m.group(1)
        for it in chunk.split(","):
            it = it.strip()
            if it:
                out.append(it)
    return out


def _sec_8k_sentiment_from_items(items: list[str]) -> float | None:
    """Aggregate per-item sentiments to a single float for an 8-K with one
    or more items. Returns the most-negative item's score (worst-case bias)
    so a Q3 beat (Item 2.02) bundled with a material impairment (Item 2.06)
    surfaces as net-negative. Returns None when no items map.
    """
    scores: list[float] = []
    for it in items:
        s = _SEC_8K_ITEM_SENTIMENT.get(it)
        if s is not None:
            scores.append(s)
    if not scores:
        return None
    return min(scores)


def collect_sec_8k(engine, *, symbols: Iterable[str], limit_per_symbol: int = 10) -> dict:
    """SEC 8-K filings via EDGAR atom feed, attributed by ticker.

    Uses the ``sec_cik_map`` to resolve ticker→CIK, then hits the EDGAR
    feed for each CIK. Item-type drives default sentiment (e.g. Item 2.06
    Material Impairment → -0.8). SEC requires a User-Agent header; we send
    the operator email per memory.
    """
    syms = list(symbols)
    if not syms:
        return {"source": "sec_8k", "written": 0, "skipped": 0, "note": "no symbols"}

    import requests as _requests
    from trading_bot.intel.sec_cik_map import get_cik_for

    written = 0
    skipped = 0
    errors = 0
    no_cik = 0
    for sym in syms:
        cik = get_cik_for(sym)
        if not cik:
            no_cik += 1
            continue
        try:
            r = _requests.get(
                "https://www.sec.gov/cgi-bin/browse-edgar",
                params={
                    "action": "getcompany",
                    "CIK": cik,
                    "type": "8-K",
                    "dateb": "",
                    "owner": "include",
                    "count": str(limit_per_symbol),
                    "output": "atom",
                },
                timeout=_PHASE_A_HTTP_TIMEOUT,
                headers={"User-Agent": _PHASE_A_USER_AGENT, "Accept": "application/atom+xml"},
            )
            r.raise_for_status()
            entries = _parse_rss_entries(r.content)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("sec_8k fetch failed for %s: %s", sym, e)
            continue

        for ent in entries:
            title = (ent.get("title") or "").strip()
            link = (ent.get("link") or "").strip()
            published = _parse_rfc822_or_iso(ent.get("published") or "")
            description = ent.get("description") or ""
            items = _parse_8k_items(f"{title} {description}")
            sentiment = _sec_8k_sentiment_from_items(items)
            ok = write_event(
                engine,
                symbol=sym.upper(),
                asset_class="stock",
                source="sec_8k",
                headline=title[:240],
                url=link,
                sentiment=sentiment,
                event_at=published,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    out = {"source": "sec_8k", "written": written, "skipped": skipped}
    if errors:
        out["errors"] = errors
    if no_cik:
        out["no_cik"] = no_cik
    return out


def _collect_per_symbol_rss(
    engine,
    *,
    source_name: str,
    symbols: Iterable[str],
    url_template: str,
) -> dict:
    """Generic per-symbol RSS collector. ``url_template`` must contain
    ``{symbol}``. Reuses the existing RSS parsing helpers.
    """
    import requests as _requests
    syms = list(symbols)
    if not syms:
        return {"source": source_name, "written": 0, "skipped": 0, "note": "no symbols"}

    written = 0
    skipped = 0
    errors = 0
    for sym in syms:
        url = url_template.format(symbol=sym)
        try:
            r = _requests.get(
                url, timeout=_PHASE_A_HTTP_TIMEOUT,
                headers={"User-Agent": _PHASE_A_USER_AGENT},
            )
            r.raise_for_status()
            entries = _parse_rss_entries(r.content)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("%s fetch failed for %s: %s", source_name, sym, e)
            continue
        for ent in entries:
            title = (ent.get("title") or "").strip()
            if not title:
                continue
            link = (ent.get("link") or "").strip()
            published = _parse_rfc822_or_iso(ent.get("published") or "")
            ok = write_event(
                engine,
                symbol=sym.upper(),
                asset_class=_guess_asset_class(sym),
                source=source_name,
                headline=title[:240],
                url=link,
                event_at=published,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    out = {"source": source_name, "written": written, "skipped": skipped}
    if errors:
        out["errors"] = errors
    return out


def collect_yahoo_rss(engine, *, symbols: Iterable[str]) -> dict:
    """Yahoo Finance per-symbol RSS feed. Free, no key, broad publisher set
    (Reuters, MarketWatch, Bloomberg headlines aggregated)."""
    return _collect_per_symbol_rss(
        engine, source_name="yahoo_rss", symbols=symbols,
        url_template="https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
    )


def collect_googlenews_rss(engine, *, symbols: Iterable[str]) -> dict:
    """GoogleNews per-symbol RSS query. Free, no key, redundant fallback
    when Yahoo is degraded; different publisher mix."""
    return _collect_per_symbol_rss(
        engine, source_name="googlenews_rss", symbols=symbols,
        url_template="https://news.google.com/rss/search?q={symbol}+stock",
    )


_REDDIT_SUBS = ("stocks", "investing", "options")


def collect_reddit_news(
    engine, *, settings, score_floor: int = 10, comments_floor: int = 20, limit: int = 50,
) -> dict:
    """Broader Reddit coverage than ApeWisdom (which only watches WSB).

    Pulls /r/stocks, /r/investing, /r/options new-post listings and
    extracts ``$TICKER`` mentions from titles + selftext. Filters to posts
    with score ≥10 OR comments ≥20 to weed out noise. Free Reddit JSON
    endpoint, ~60 req/min anonymous.
    """
    import requests as _requests
    user_agent = getattr(settings, "reddit_user_agent", "") or _PHASE_A_USER_AGENT

    written = 0
    skipped = 0
    errors = 0
    for sub in _REDDIT_SUBS:
        try:
            r = _requests.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                params={"limit": str(limit)},
                timeout=_PHASE_A_HTTP_TIMEOUT,
                headers={"User-Agent": user_agent},
            )
            r.raise_for_status()
            body = r.json() or {}
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("reddit_news fetch failed for r/%s: %s", sub, e)
            continue
        for child in (body.get("data", {}).get("children") or []):
            post = child.get("data") or {}
            score = int(post.get("score") or 0)
            n_comments = int(post.get("num_comments") or 0)
            if score < score_floor and n_comments < comments_floor:
                continue
            title = (post.get("title") or "").strip()
            selftext = (post.get("selftext") or "").strip()
            tickers = _extract_tickers_from_text(f"{title} {selftext}")
            if not tickers:
                continue
            permalink = post.get("permalink") or ""
            url = f"https://www.reddit.com{permalink}" if permalink else ""
            created = post.get("created_utc")
            event_at = (
                dt.datetime.fromtimestamp(float(created), tz=dt.timezone.utc)
                if created else None
            )
            for sym in tickers:
                ok = write_event(
                    engine,
                    symbol=sym.upper(),
                    asset_class=_guess_asset_class(sym),
                    source="reddit_news",
                    headline=title[:240],
                    url=url,
                    raw_score=float(score),
                    event_at=event_at,
                )
                if ok:
                    written += 1
                else:
                    skipped += 1
    out = {"source": "reddit_news", "written": written, "skipped": skipped}
    if errors:
        out["errors"] = errors
    return out


# NewsAPI free tier = 100 req/day total. With ~48 ingestor ticks/day,
# unrestricted per-tick calling burns the quota in <2 hours. We gate the
# collector to a single tick per day (UTC hour 14, ~10 ET / 9 ET DST)
# so the daily budget covers ~50 symbols per backfill. Operator can skip
# the gate by passing ``force=True`` (used by tests / one-off backfills).
_NEWSAPI_WINDOW_UTC_HOUR = 14


def _newsapi_window_open(now: dt.datetime | None = None) -> bool:
    n = now or dt.datetime.now(dt.timezone.utc)
    return n.hour == _NEWSAPI_WINDOW_UTC_HOUR


def collect_newsapi(
    engine, *, settings, symbols: Iterable[str],
    limit_per_symbol: int = 5, force: bool = False,
) -> dict:
    """NewsAPI.org per-symbol search. Free tier 100 req/day.

    Daily-backfill gate (``_newsapi_window_open``) bounds calls to a single
    tick per day to stay inside the quota. Caller passing ``force=True``
    bypasses the gate. Empty key → silent skip.
    """
    if not force and not _newsapi_window_open():
        return {"source": "newsapi", "written": 0, "skipped": 0, "note": "outside daily window"}
    syms = list(symbols)
    if not syms:
        return {"source": "newsapi", "written": 0, "skipped": 0, "note": "no symbols"}
    api_key = getattr(settings, "newsapi_key", "") or ""
    if not api_key:
        return {"source": "newsapi", "written": 0, "skipped": 0, "note": "no api key"}

    import requests as _requests
    written = 0
    skipped = 0
    errors = 0
    since = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    ).date().isoformat()
    for sym in syms:
        try:
            r = _requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": sym,
                    "from": since,
                    "language": "en",
                    "sortBy": "publishedAt",
                    "pageSize": str(limit_per_symbol),
                    "apiKey": api_key,
                },
                timeout=_PHASE_A_HTTP_TIMEOUT,
                headers={"User-Agent": _PHASE_A_USER_AGENT},
            )
            if r.status_code == 429:
                errors += 1
                log.warning("newsapi rate-limited on %s; stopping batch", sym)
                break
            r.raise_for_status()
            body = r.json() or {}
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.warning("newsapi fetch failed for %s: %s", sym, e)
            continue
        for art in (body.get("articles") or []):
            title = (art.get("title") or "").strip()
            if not title:
                continue
            url = (art.get("url") or "").strip()
            published = _parse_rfc822_or_iso(art.get("publishedAt") or "")
            ok = write_event(
                engine,
                symbol=sym.upper(),
                asset_class=_guess_asset_class(sym),
                source="newsapi",
                headline=title[:240],
                url=url,
                event_at=published,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    out = {"source": "newsapi", "written": written, "skipped": skipped}
    if errors:
        out["errors"] = errors
    return out


def collect_apewisdom(engine) -> dict:
    """ApeWisdom WSB mentions. ``is_spike`` was the pre-existing read API;
    here we record raw mention counts so the aggregator can spike-detect
    on its own."""
    from trading_bot.intelligence_apewisdom import ApeWisdomClient
    written = 0
    skipped = 0
    try:
        client = ApeWisdomClient()
        mentions = client.wallstreetbets_mentions()
    except Exception as e:  # noqa: BLE001
        log.warning("apewisdom collect failed: %s", e)
        return {"source": "apewisdom", "written": 0, "skipped": 0, "error": str(e)}
    for sym, row in mentions.items():
        if not sym or sym.startswith("$"):
            sym = sym.lstrip("$")
        if not sym or "/" in sym:
            continue
        # Only write for "elevated" mentions — single-mention noise is
        # a worse-than-nothing signal. Threshold = 5 mentions.
        if row.mentions < 5:
            continue
        delta = row.mentions - row.mentions_24h_ago
        ok = write_event(
            engine,
            symbol=sym.upper(),
            asset_class=_guess_asset_class(sym),
            source="apewisdom",
            headline=f"{sym} WSB cluster: {row.mentions} mentions ({delta:+d} vs 24h ago, rank {row.rank})",
            raw_score=float(row.mentions),
        )
        if ok:
            written += 1
        else:
            skipped += 1
    return {"source": "apewisdom", "written": written, "skipped": skipped}


def collect_vip_tweets(engine) -> dict:
    """VIP Twitter/Truth-Social watchlist.

    The ``vip_listener`` role already polls + persists posts. We re-poll
    here (lightweight RSS) and extract $TICKER mentions from each post's
    text. Severity drives whether we write at all — only "high" /
    "medium" posts get recorded since "low" is ambient chatter.
    """
    try:
        from trading_bot.vip_tweets import (
            VIP_HANDLES_PATH, load_handles, fetch_handle_posts,
        )
    except Exception as e:  # noqa: BLE001
        return {"source": "vip_tweet", "written": 0, "skipped": 0, "error": str(e)}
    written = 0
    skipped = 0
    try:
        handles = load_handles(VIP_HANDLES_PATH)
        if not handles:
            return {"source": "vip_tweet", "written": 0, "skipped": 0, "note": "no handles"}
        posts: list = []
        for h in handles:
            try:
                posts.extend(fetch_handle_posts(h))
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        log.warning("vip_tweets collect failed: %s", e)
        return {"source": "vip_tweet", "written": 0, "skipped": 0, "error": str(e)}
    for post in posts:
        if getattr(post, "severity", "low") == "low":
            continue
        tickers = _extract_tickers_from_text(getattr(post, "text", ""))
        for sym in tickers:
            ok = write_event(
                engine,
                symbol=sym.upper(),
                asset_class=_guess_asset_class(sym),
                source="vip_tweet",
                headline=(post.text or "")[:240],
                url=getattr(post, "url", "") or "",
                event_at=getattr(post, "published", None),
            )
            if ok:
                written += 1
            else:
                skipped += 1
    return {"source": "vip_tweet", "written": written, "skipped": skipped}


_TICKER_PATTERN = None


def _extract_tickers_from_text(text: str) -> list[str]:
    """Heuristic ticker extraction. Looks for $XYZ or 'NASDAQ:XYZ' patterns
    plus standalone uppercase 1-5 char tokens that appear in our universe.
    Returns deduped uppercase tickers.

    This is a noise floor — we don't need perfect recall. Real ticker
    extraction across free-form text is hard; we accept missing some
    mentions in exchange for not pumping garbage symbols into the pool.
    """
    import re
    global _TICKER_PATTERN
    if _TICKER_PATTERN is None:
        _TICKER_PATTERN = re.compile(r"\$([A-Z]{1,5})\b|\b(?:NASDAQ|NYSE):([A-Z]{1,5})\b")
    out: set[str] = set()
    for match in _TICKER_PATTERN.finditer(text or ""):
        sym = (match.group(1) or match.group(2) or "").upper()
        if sym:
            out.add(sym)
    return sorted(out)


def collect_finnhub_news(
    engine, *, settings, symbols: Iterable[str]
) -> dict:
    """Finnhub company news. Disabled-by-default stub: the existing
    FinnhubClient on this codebase doesn't expose a news endpoint
    (only earnings + company_profile). Kept here as a documented hook
    for a future ``company_news`` method."""
    return {"source": "finnhub_news", "written": 0, "skipped": 0, "note": "endpoint not wired"}


# ---------------------------------------------------------------------------
# Phase 6 — crypto-specific sources
# ---------------------------------------------------------------------------
# These four collectors were added to fix the "starved crypto pool" gap:
# pre-Phase-6 the only writers for asset_class='crypto' were Alpaca News
# (sparse for crypto) and the rare ambient VIP tweet. The intel_score for
# every crypto symbol sat near zero, which broke the regime-override path.
#
# All four are best-effort: outages return zero-written, never raise.
# ---------------------------------------------------------------------------

_CRYPTO_RSS_TIMEOUT = 10
_CRYPTO_USER_AGENT = "TradingBot/1.0 (+bharath8887@gmail.com)"


def collect_apewisdom_crypto(engine) -> dict:
    """ApeWisdom r/CryptoCurrency mentions as a *source* feeding the pool.

    ``intel_gates._fetch_crypto_mentions()`` already wraps this endpoint
    for the spike-skip gate. We re-fetch here (small, cached) and write
    one event per coin with mentions ≥ 5 — same threshold as the equity
    apewisdom collector.
    """
    written = 0
    skipped = 0
    try:
        from trading_bot.intel_gates import _fetch_crypto_mentions
        snap = _fetch_crypto_mentions()
    except Exception as e:  # noqa: BLE001
        log.warning("apewisdom_crypto collect failed: %s", e)
        return {"source": "apewisdom_crypto", "written": 0, "skipped": 0, "error": str(e)}
    if not snap:
        return {"source": "apewisdom_crypto", "written": 0, "skipped": 0,
                "note": "no data"}
    for ticker, row in snap.items():
        mentions = int(row.get("mentions") or 0)
        if mentions < 5:
            continue
        prior = int(row.get("mentions_24h_ago") or 0)
        delta = mentions - prior
        rank = int(row.get("rank") or 999)
        ok = write_event(
            engine,
            symbol=ticker.upper(),
            asset_class="crypto",
            source="apewisdom_crypto",
            headline=(
                f"{ticker} r/CryptoCurrency cluster: {mentions} mentions "
                f"({delta:+d} vs 24h ago, rank {rank})"
            ),
            raw_score=float(mentions),
        )
        if ok:
            written += 1
        else:
            skipped += 1
    return {"source": "apewisdom_crypto", "written": written, "skipped": skipped}


def _parse_rss_entries(xml_bytes: bytes) -> list[dict]:
    """Tiny stdlib RSS parser. Extracts (title, link, pubDate, description)
    per <item>. Atom feeds use <entry>/<id>/<published>/<summary>; we
    cover both. No fancy XML parsing — we only need a few text fields."""
    import xml.etree.ElementTree as ET
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out

    def _strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    for item in root.iter():
        if _strip_ns(item.tag) not in ("item", "entry"):
            continue
        rec: dict = {"title": "", "link": "", "published": "", "description": ""}
        for child in item:
            t = _strip_ns(child.tag)
            if t == "title":
                rec["title"] = (child.text or "").strip()
            elif t == "link":
                href = child.get("href")
                rec["link"] = (href or child.text or "").strip()
            elif t in ("pubDate", "published", "updated"):
                rec["published"] = (child.text or "").strip()
            elif t in ("description", "summary"):
                rec["description"] = (child.text or "").strip()
        if rec["title"] or rec["link"]:
            out.append(rec)
    return out


def _parse_rfc822_or_iso(s: str):
    """Best-effort parse: RSS uses RFC 822, Atom uses ISO 8601. Returns
    timezone-aware datetime or None. We never raise here — a missing
    ``published`` field is fine; the aggregator falls back to ingested_at."""
    if not s:
        return None
    import datetime as _dt
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_crypto_rss(engine, *, source_name: str, url: str) -> dict:
    """Generic RSS-to-events for editorial crypto sources. Extracts
    crypto symbols from each headline+description via the slug map and
    writes one event per (article × symbol) pair. Dedup on (source, url)
    handled by the aggregator's unique index."""
    import requests
    from trading_bot.intel._crypto_symbols import extract_symbols_from_text

    written = 0
    skipped = 0
    try:
        r = requests.get(
            url, timeout=_CRYPTO_RSS_TIMEOUT,
            headers={"User-Agent": _CRYPTO_USER_AGENT},
        )
        r.raise_for_status()
        entries = _parse_rss_entries(r.content)
    except Exception as e:  # noqa: BLE001
        log.warning("%s collect failed: %s", source_name, e)
        return {"source": source_name, "written": 0, "skipped": 0, "error": str(e)}

    for ent in entries:
        text = f"{ent.get('title','')} {ent.get('description','')}"
        symbols = extract_symbols_from_text(text)
        if not symbols:
            continue
        published = _parse_rfc822_or_iso(ent.get("published", ""))
        link = ent.get("link", "") or ""
        title = ent.get("title", "") or ""
        for sym in symbols:
            ok = write_event(
                engine,
                symbol=f"{sym}/USD",  # canonicalize to trading pair the bot uses
                asset_class="crypto",
                source=source_name,
                headline=title[:240],
                url=link,
                event_at=published,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    return {"source": source_name, "written": written, "skipped": skipped}


def collect_coindesk_rss(engine) -> dict:
    """CoinDesk RSS — broad, editorial crypto news. No auth, stable feed."""
    return _collect_crypto_rss(
        engine,
        source_name="coindesk_rss",
        url="https://www.coindesk.com/arc/outboundfeeds/rss/",
    )


def collect_cointelegraph_rss(engine) -> dict:
    """CoinTelegraph RSS — second editorial feed. Cross-source bonus
    kicks in when both CoinDesk + CoinTelegraph carry the same story."""
    return _collect_crypto_rss(
        engine,
        source_name="cointelegraph_rss",
        url="https://cointelegraph.com/rss",
    )


def collect_cryptopanic(engine, *, settings) -> dict:
    """CryptoPanic free API: aggregator across 100+ crypto news sources
    plus community vote sentiment. Free tier 200 req/day; we call once
    per role tick. Empty key → silent skip (source is opt-in)."""
    import requests
    api_key = getattr(settings, "cryptopanic_api_key", "") or ""
    if not api_key:
        return {"source": "cryptopanic", "written": 0, "skipped": 0,
                "note": "no api key"}
    written = 0
    skipped = 0
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={
                "auth_token": api_key,
                "kind": "news",
                "public": "true",
            },
            timeout=_CRYPTO_RSS_TIMEOUT,
            headers={"User-Agent": _CRYPTO_USER_AGENT},
        )
        r.raise_for_status()
        body = r.json() or {}
    except Exception as e:  # noqa: BLE001
        log.warning("cryptopanic collect failed: %s", e)
        return {"source": "cryptopanic", "written": 0, "skipped": 0, "error": str(e)}

    for post in (body.get("results") or []):
        title = (post.get("title") or "").strip()
        url = (post.get("url") or "").strip()
        published = _parse_rfc822_or_iso(post.get("published_at") or "")
        # Community-vote sentiment: positive votes - negative votes,
        # normalised to [-1, +1]. 'important' acts as a magnitude boost
        # (folded into raw_score for transparency).
        votes = post.get("votes") or {}
        pos = int(votes.get("positive") or 0)
        neg = int(votes.get("negative") or 0)
        important = int(votes.get("important") or 0)
        total_dir = pos + neg
        sentiment = (pos - neg) / total_dir if total_dir > 0 else 0.0
        sentiment = max(-1.0, min(1.0, sentiment))
        for cur in (post.get("currencies") or []):
            sym = (cur.get("code") or "").upper().strip()
            if not sym:
                continue
            ok = write_event(
                engine,
                symbol=f"{sym}/USD",
                asset_class="crypto",
                source="cryptopanic",
                headline=title[:240],
                url=url,
                sentiment=sentiment,
                raw_score=float(important) if important else None,
                event_at=published,
            )
            if ok:
                written += 1
            else:
                skipped += 1
    return {"source": "cryptopanic", "written": written, "skipped": skipped}


def collect_all(
    engine, *, settings, seed_symbols: Iterable[str] | None = None,
    elevated_min_score: float = 2.0, elevated_limit: int = 30,
) -> list[dict]:
    """Run every wired source IN STRICT SEQUENTIAL ORDER. Returns the per-
    source summary list so the role can log it / persist it / surface on
    the dashboard.

    Sequencing rationale (Phase A):
      1. Broad per-symbol news first (Alpaca) — populates initial events
      2. Legacy stubs second (sec_form4, finnhub, gdelt) — no-ops, kept
         for shape stability
      3. Broad social/sub scans (apewisdom, vip_tweets) — extract tickers
         from free-text feeds; no symbol gating needed
      4. Phase A high-trust filings (sec_8k) — run on full seed; legal
         filings drive elevation, can't be gated by it
      5. Phase A broad-sub social (reddit_news) — broader than apewisdom
      6. Compute elevated set from PRIOR tick's intel_candidates
      7. Phase A rate-limited / politeness-bounded sources (polygon_news,
         yahoo_rss, googlenews_rss, newsapi) — only call for elevated names
      8. Phase 6 crypto sources

    Failure isolation: each ``collect_*`` traps its own exceptions.
    """
    seed = list(seed_symbols or [])
    out = []

    # 1. Broad per-symbol stock news
    out.append(collect_alpaca_news(engine, settings=settings, symbols=seed))

    # 2. Legacy stubs (kept for output-shape stability)
    out.append(collect_sec_form4(engine))
    out.append(collect_finnhub_news(engine, settings=settings, symbols=seed))
    out.append(collect_gdelt(engine))

    # 3. Broad social / sub scans
    out.append(collect_apewisdom(engine))
    out.append(collect_vip_tweets(engine))

    # 4. Phase A — high-trust SEC 8-K filings (full seed)
    out.append(collect_sec_8k(engine, symbols=seed))

    # 5. Phase A — broader-sub Reddit (r/stocks, r/investing, r/options)
    out.append(collect_reddit_news(engine, settings=settings))

    # 6. Compute elevated set from prior tick's intel_candidates.
    #    Cold-start tick: empty list → elevated-only collectors no-op,
    #    next tick they'll fire after roll_up populates candidates.
    elevated = _elevated_symbols(
        engine, asset_class="stock",
        min_score=elevated_min_score, limit=elevated_limit,
    )

    # 7. Phase A — rate-limited / politeness-bounded sources
    out.append(collect_massive_news(engine, settings=settings, symbols=elevated))
    out.append(collect_yahoo_rss(engine, symbols=elevated))
    out.append(collect_googlenews_rss(engine, symbols=elevated))
    out.append(collect_newsapi(engine, settings=settings, symbols=elevated[:10]))

    # 8. Phase 6 — crypto-specific sources
    out.append(collect_apewisdom_crypto(engine))
    out.append(collect_coindesk_rss(engine))
    out.append(collect_cointelegraph_rss(engine))
    out.append(collect_cryptopanic(engine, settings=settings))
    return out


_CRYPTO_QUOTES = ("/USD", "/USDT", "/USDC", "/EUR", "/BTC")


def _guess_asset_class(symbol: str) -> str:
    """Heuristic: 'BTC/USD'-style → crypto; otherwise stock.

    Be conservative: ``BRK/A`` and ``BRK/B`` (Berkshire share classes from
    some quote feeds) contain a slash but are NOT crypto. We require one
    of the recognized crypto quote-currency suffixes. The wheel/options
    lane reads asset_class='stock' and does its own optionable check, so
    we don't need a separate option_underlying tag at write time.
    """
    if not symbol:
        return "stock"
    s = symbol.upper()
    if any(s.endswith(q) for q in _CRYPTO_QUOTES):
        return "crypto"
    return "stock"
