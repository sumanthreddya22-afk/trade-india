"""Crypto universe discovery.

Pulls Alpaca's active+tradable crypto asset list, filters to USD-quoted
pairs, excludes stablecoins (no momentum to trade), applies the
operator blocklist. Returns WatchlistEntry list.

This is the crypto analogue of the equity screener pipeline:
  * massive_refresh + universe.curate → liquid US stocks
  * wheel_universe_builder + Finnhub filter → optionable equities ≥ $10B
  * crypto_universe.discover → tradable USD-quoted crypto, no stablecoins

Alpaca's paper account currently lists ~36 USD-quoted tradable pairs.
After stablecoin filter that's ~30 names. The momentum lane decides
which to trade per scan.

Operator override:
  strategy/crypto_blocklist.yaml — never scan these (e.g., low-quality
  memecoins you want to skip)."""
from __future__ import annotations

import logging

from trading_bot.shared.alpaca_client import AlpacaClient
from trading_bot.state import WatchlistEntry


log = logging.getLogger(__name__)


# Stablecoins — pegged ~$1, won't generate momentum signals so a scan is
# wasted CPU. PAXG is gold-backed (commodity proxy, not a trade target).
STABLECOINS: frozenset[str] = frozenset({
    "USDC/USD", "USDT/USD", "USDG/USD", "USDP/USD", "GUSD/USD",
    "DAI/USD", "BUSD/USD", "TUSD/USD", "FRAX/USD", "PAXG/USD",
    "FDUSD/USD", "PYUSD/USD",
})


def is_stablecoin(symbol: str) -> bool:
    return symbol.upper() in STABLECOINS


def discover_crypto_universe(
    alpaca_client: AlpacaClient, *,
    blocklist: set[str] | None = None,
) -> list[WatchlistEntry]:
    """Discover the active crypto trading universe from Alpaca.

    Returns WatchlistEntry list ready for the orchestrator. Filters:
      1. asset_class = crypto, tradable = True (Alpaca-side)
      2. USD-quoted only (BTC/USD, not BTC/EUR or ETH/BTC)
      3. Not a stablecoin
      4. Not in operator blocklist

    Network/API failure returns []  — the scan is then a clean no-op.
    """
    block = {s.upper() for s in (blocklist or set())}
    try:
        raw = alpaca_client.get_active_assets("crypto")
    except Exception as e:  # noqa: BLE001 — best-effort discovery
        log.warning("crypto universe discovery failed: %s", e)
        return []

    out: list[WatchlistEntry] = []
    for a in raw:
        sym = a.symbol.upper()
        if not a.tradable:
            continue
        if not sym.endswith("/USD"):
            continue
        if is_stablecoin(sym):
            continue
        if sym in block:
            continue
        out.append(WatchlistEntry(
            symbol=sym,
            asset_class="crypto",
            notes="auto-discovered",
        ))
    return out
