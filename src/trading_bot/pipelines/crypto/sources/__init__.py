"""Crypto pipeline source collectors + orchestration.

Each ``collect_*`` function pulls from one external source (REST API,
RSS, WebSocket, on-chain poll) and writes ``IntelEventCrypto`` rows
through ``_base.write_event``. Failure of one source never affects
the others — every collector traps its own exceptions and reports the
count of events written/skipped via a ``SourceResult``.

The ``collect_all`` function in this module orchestrates a single tick
across all 11 sources sequentially (per ADR 0003 — strict sequential
within a decision path; sources are part of one decision tick). Total
wall-clock per tick: ~5-30s depending on which sources hit cache vs.
live API.

Sources (Phase 1A — 7 new + 4 ported):

  Tier-1 (highest signal, weight 5.0):
    whale_alert       — Whale Alert REST poll for >$1M BTC/ETH transfers
    exchange_listings — Coinbase + Binance new-listing announcements
    rekt_news         — Rekt.news exploit RSS

  Tier-2 (medium signal):
    etherscan_whales         — Etherscan ERC-20 large-transfer poll  (3.0)
    token_unlocks_defillama  — TokenUnlocks + DefiLlama TVL          (3.0/2.0)
    coindesk_rss             — CoinDesk editorial RSS                (2.5)
    cryptopanic              — CryptoPanic aggregator                (2.5)
    snapshot_governance      — Snapshot.org DAO votes                (2.5)

  Tier-3 (signal floor):
    cointelegraph_rss — CoinTelegraph editorial RSS                  (2.0)
    apewisdom_crypto  — r/CryptoCurrency mentions                    (2.0)
    binance_funding   — Binance perp funding rates                   (2.0)
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, List, Optional

from trading_bot.pipelines.crypto.sources._base import SourceResult, utcnow

# Re-export the individual collectors so callers can import them directly
# (e.g., `from trading_bot.pipelines.crypto.sources import collect_whale_alert`).
from trading_bot.pipelines.crypto.sources.whale_alert import collect_whale_alert
from trading_bot.pipelines.crypto.sources.etherscan_whales import collect_etherscan_whales
from trading_bot.pipelines.crypto.sources.exchange_listings import collect_exchange_listings
from trading_bot.pipelines.crypto.sources.rekt_news import collect_rekt_news
from trading_bot.pipelines.crypto.sources.snapshot_governance import collect_snapshot_governance
from trading_bot.pipelines.crypto.sources.binance_funding import collect_binance_funding
from trading_bot.pipelines.crypto.sources.token_unlocks_defillama import (
    collect_token_unlocks_defillama,
)
from trading_bot.pipelines.crypto.sources.apewisdom_crypto import collect_apewisdom_crypto
from trading_bot.pipelines.crypto.sources.coindesk_rss import collect_coindesk_rss
from trading_bot.pipelines.crypto.sources.cointelegraph_rss import collect_cointelegraph_rss
from trading_bot.pipelines.crypto.sources.cryptopanic import collect_cryptopanic

__all__ = [
    "collect_all",
    "ALL_COLLECTORS",
    # individual collectors
    "collect_whale_alert",
    "collect_etherscan_whales",
    "collect_exchange_listings",
    "collect_rekt_news",
    "collect_snapshot_governance",
    "collect_binance_funding",
    "collect_token_unlocks_defillama",
    "collect_apewisdom_crypto",
    "collect_coindesk_rss",
    "collect_cointelegraph_rss",
    "collect_cryptopanic",
]

logger = logging.getLogger(__name__)


# Strict sequential order. Tier-1 first so a budget-limited tick still
# captures the highest-signal sources before Tier-3 noise. Within a tier,
# free-tier sources run before keyed ones so an empty key doesn't delay
# the keyed source.
ALL_COLLECTORS: List[tuple[str, Callable[..., SourceResult]]] = [
    # Tier-1 (signal 5.0)
    ("whale_alert",             collect_whale_alert),
    ("exchange_listings",       collect_exchange_listings),
    ("rekt_news",               collect_rekt_news),
    # Tier-2
    ("etherscan_whales",        collect_etherscan_whales),
    ("token_unlocks_defillama", collect_token_unlocks_defillama),
    ("coindesk_rss",            collect_coindesk_rss),
    ("cryptopanic",             collect_cryptopanic),
    ("snapshot_governance",     collect_snapshot_governance),
    # Tier-3
    ("cointelegraph_rss",       collect_cointelegraph_rss),
    ("apewisdom_crypto",        collect_apewisdom_crypto),
    ("binance_funding",         collect_binance_funding),
]


def collect_all(
    engine: Any,
    *,
    settings: Any,
    skip: Optional[List[str]] = None,
    only: Optional[List[str]] = None,
    now: Optional[dt.datetime] = None,
) -> List[Dict[str, Any]]:
    """Run every wired crypto source sequentially. Returns one dict per
    source with the per-source written/skipped/error counts.

    ``settings`` is the global ``Settings`` object (env-loaded API keys);
    each collector picks the keys it needs (``whale_alert_api_key`` etc.)
    or returns a no-op SourceResult when its key is missing.

    ``skip`` / ``only`` let callers narrow the run for ad-hoc CLI use.
    Empty / None means "every source".

    Per ADR 0003 (optimistic concurrency, strict sequential within a
    decision path) sources run one-at-a-time. A failing source contributes
    an ``error`` field but does not stop the next source from running.
    """
    now = now or utcnow()
    skip_set = set(skip or [])
    only_set = set(only or [])
    out: List[Dict[str, Any]] = []

    for name, collector in ALL_COLLECTORS:
        if only_set and name not in only_set:
            continue
        if name in skip_set:
            continue
        try:
            result = _invoke_collector(collector, engine=engine, settings=settings, now=now)
        except TypeError:
            # Backwards-compat: some collectors don't accept all kwargs.
            try:
                result = collector(engine, settings=settings)
            except Exception as e:  # noqa: BLE001
                logger.warning("collect_all %s crashed: %s", name, e)
                result = SourceResult(source=name, error=str(e))
        except Exception as e:  # noqa: BLE001
            logger.warning("collect_all %s crashed: %s", name, e)
            result = SourceResult(source=name, error=str(e))

        out.append(result.as_dict() if isinstance(result, SourceResult) else result)
    return out


def _invoke_collector(collector, *, engine, settings, now):
    """Call a collector with the unified kwargs.

    Most collectors accept ``settings`` and ``now``; a few only accept
    ``settings`` (apewisdom_crypto, coindesk_rss, etc.). We try the rich
    signature first and let TypeError fall through to the simpler call
    in ``collect_all``.
    """
    return collector(engine, settings=settings, now=now)
