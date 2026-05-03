"""Crypto-source collector framework — write_event + SourceResult + helpers.

Mirrors ``trading_bot.intel.aggregator.write_event`` shape so the
existing aggregator/scoring patterns port over with minimal change,
but writes to ``intel_events_crypto`` instead of ``intel_events``.

Per-pipeline isolation: this module never imports anything from the
stocks pipeline. It writes only to crypto-owned tables.

Source weights + decay constants live here too — alongside the writer
so they're visible in one place. Phase 1E (adaptive thresholds) will
allow per-source weight overrides via the same threshold_overrides
mechanism the stocks side uses.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import IntelEventCrypto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source weights — Phase 1A baseline (tunable in Phase 1E adaptive thresholds)
# ---------------------------------------------------------------------------

CRYPTO_SOURCE_WEIGHTS: Dict[str, float] = {
    # Tier-1 (highest signal)
    "whale_alert":         5.0,   # confirmed >$1M on-chain transfers
    "exchange_listing":    5.0,   # Coinbase + Binance listings = "Coinbase effect"
    "rekt_exploit":        5.0,   # exploit/hack confirmed by Rekt.news / PeckShield
    # Tier-2 (medium)
    "etherscan_whales":    3.0,   # ERC-20 large-transfer fallback / depth
    "token_unlocks":       3.0,   # deterministic supply shocks
    "coindesk_rss":        2.5,   # editorial, low-noise
    "cryptopanic":         2.5,   # 100+ source aggregator + community vote
    "snapshot_governance": 2.5,   # off-chain DAO votes (UNI, AAVE, etc.)
    # Tier-3 (signal floor)
    "cointelegraph_rss":   2.0,   # editorial
    "apewisdom_crypto":    2.0,   # r/CryptoCurrency mentions
    "binance_funding":     2.0,   # extreme funding signals over-leverage
    "defillama_tvl":       2.0,   # protocol health
}


CRYPTO_DECAY_HOURS: Dict[str, float] = {
    # Tier-1 — high signal stays relevant longer
    "whale_alert":         12.0,  # whale flow signal half-life
    "exchange_listing":    24.0,  # listing pump usually plays out within 24h
    "rekt_exploit":        24.0,  # exploit aftermath stays material for a day
    # Tier-2
    "etherscan_whales":     8.0,
    "token_unlocks":      168.0,  # unlock signals arc over a week
    "coindesk_rss":        12.0,
    "cryptopanic":          8.0,  # aggregator posts age faster than editorials
    "snapshot_governance": 72.0,  # DAO proposals decided over days
    # Tier-3
    "cointelegraph_rss":   12.0,
    "apewisdom_crypto":    24.0,  # community discussion lifespan
    "binance_funding":      4.0,  # funding flips are fast-moving
    "defillama_tvl":       12.0,
}


DEFAULT_SOURCE_WEIGHT = 1.0
DEFAULT_DECAY_HOURS = 12.0


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class SourceResult:
    """Per-collector return value. ``error`` is set on failure but the
    collector still returns rather than raising so collect_all keeps going.
    """
    source: str
    written: int = 0
    skipped: int = 0
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "source": self.source,
            "written": self.written,
            "skipped": self.skipped,
        }
        if self.error:
            d["error"] = self.error
        if self.extra:
            d["extra"] = self.extra
        return d


# ---------------------------------------------------------------------------
# write_event — the only path crypto data goes into the DB
# ---------------------------------------------------------------------------


def write_event(
    engine: Any,
    *,
    symbol: str,
    source: str,
    headline: str = "",
    url: str = "",
    sentiment: Optional[float] = None,
    raw_score: Optional[float] = None,
    event_at: Optional[dt.datetime] = None,
    event_hash: Optional[str] = None,
    chain: Optional[str] = None,
    tx_hash: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> bool:
    """Insert one ``IntelEventCrypto`` row. Idempotent via the
    (symbol, source, event_hash) unique constraint — duplicates return
    False without raising.

    ``event_hash`` defaults to SHA1 of source+url (or source+headline
    when no URL is given). Pass an explicit value when the source
    provides a stable id (tx_hash, post_id, message_id, etc.) so the
    dedup window survives URL canonicalisation drift.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if event_hash is None:
        h = hashlib.sha1()
        h.update(source.encode())
        h.update((url or headline or "").encode())
        event_hash = h.hexdigest()

    row = IntelEventCrypto(
        symbol=symbol.upper(),
        source=source,
        headline=(headline or "")[:1000],
        url=(url or "")[:1000],
        sentiment=sentiment,
        raw_score=raw_score,
        event_at=event_at,
        ingested_at=now,
        event_hash=event_hash,
        chain=chain,
        tx_hash=tx_hash,
    )
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            return True
    except IntegrityError:
        return False


# ---------------------------------------------------------------------------
# Helpers shared across collectors
# ---------------------------------------------------------------------------


_STABLECOIN_TOKENS = frozenset({"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USD"})


def normalize_crypto_symbol(raw: str) -> str:
    """Normalise a crypto ticker / pair to the canonical ``BTC/USD`` form.

    Inputs we accept:
      "BTC", "btc", "btcusd", "BTC-USD", "BTCUSDT", "BTC/USD", "BTC/USDT"
    Returns:
      "BTC/USD" (default quote when only base ticker is given)

    Stablecoin pairs are normalised to /USD because all our trading
    happens against USD anyway. ``USDT``, ``USDC`` and ``BUSD`` get
    folded into ``USD`` on the QUOTE side so cross-source dedup works.

    Stablecoin TOKENS themselves (``BUSD``, ``USDT``, etc.) are preserved
    whole — a delisting headline about BUSD is about the BUSD token, not
    "B" with a USD suffix.
    """
    s = (raw or "").upper().strip()
    if not s:
        return s
    # Fast path: already canonical
    if "/" in s:
        base, _, quote = s.partition("/")
        return f"{base}/{_canonical_quote(quote)}"
    # Stripped delimiters
    s = s.replace("-", "").replace("_", "")
    # Stablecoin token names stay intact (don't strip "USD" off "BUSD")
    if s in _STABLECOIN_TOKENS:
        return f"{s}/USD"
    # Try common stable-coin suffixes
    for suffix in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
        if s.endswith(suffix) and s != suffix:
            base = s[: -len(suffix)]
            if base:
                return f"{base}/{_canonical_quote(suffix)}"
    # Pure base ticker — default to /USD
    return f"{s}/USD"


def _canonical_quote(q: str) -> str:
    q = q.upper()
    if q in ("USDT", "USDC", "BUSD"):
        return "USD"
    return q


def stable_event_hash(*parts: str) -> str:
    """Compose a deterministic hash from arbitrary parts.

    Used by sources that have a natural unique id (tx_hash, post_id,
    proposal_id) so the dedup constraint survives URL drift.
    """
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------------
# RSS helpers (shared by coindesk, cointelegraph, rekt_news, exchange_listings)
# ---------------------------------------------------------------------------


def parse_rss_entries(xml_bytes: bytes) -> list[dict]:
    """Tiny stdlib RSS / Atom parser.

    Extracts ``title`` / ``link`` / ``published`` / ``description`` per item.
    Atom feeds use ``<entry>`` / ``<id>`` / ``<published>`` / ``<summary>``;
    we cover both. No fancy XML — only a few text fields are needed.
    Ported from ``trading_bot.intel.sources._parse_rss_entries`` so the
    crypto pipeline doesn't import from the stocks tree.
    """
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


def parse_rfc822_or_iso(s: str) -> Optional[dt.datetime]:
    """Best-effort timestamp parse.

    RSS uses RFC 822 (``Wed, 02 May 2026 10:00:00 +0000``); Atom uses
    ISO 8601 (``2026-05-02T10:00:00Z``). Returns timezone-aware datetime
    or None. Never raises — a missing ``published`` field is fine; the
    aggregator falls back to ingested_at.
    """
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


CRYPTO_USER_AGENT = "TradingBot/1.0 (+bharath8887@gmail.com)"
CRYPTO_RSS_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Source registry — used by collect_all + the persona-versioning audit
# ---------------------------------------------------------------------------


@dataclass
class SourceSpec:
    """Declarative metadata for one crypto source.

    The orchestration layer (``pipelines/crypto/sources/__init__.py``
    Phase 1A.11) iterates over the registry and calls each ``collector``.
    Adding a new source means: write the collector, register it here,
    set the weight + decay above. No other call sites change.
    """
    name: str
    collector: Any                 # Callable[[engine, **kwargs], SourceResult]
    weight: float
    decay_hours: float
    requires_keys: tuple[str, ...] = ()
    description: str = ""


SOURCE_REGISTRY: Dict[str, SourceSpec] = {}


def register_source(spec: SourceSpec) -> None:
    """Register a source so collect_all picks it up."""
    if spec.name in SOURCE_REGISTRY:
        raise ValueError(f"crypto source already registered: {spec.name!r}")
    if spec.name not in CRYPTO_SOURCE_WEIGHTS:
        raise ValueError(
            f"crypto source {spec.name!r} has no weight — add it to "
            f"CRYPTO_SOURCE_WEIGHTS before registering"
        )
    SOURCE_REGISTRY[spec.name] = spec
