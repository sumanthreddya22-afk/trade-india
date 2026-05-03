"""Slug → ticker map for crypto news source extraction.

RSS feeds (CoinDesk, CoinTelegraph) write headlines as "Bitcoin", "Ethereum",
"Solana" etc. — full canonical names — not the trading tickers. This map
canonicalises name → ticker so the intel pool gets ``BTC``, ``ETH``, ``SOL``
rows instead of unmappable noise.

Conservative on purpose: we'd rather drop an event than mis-tag it. Operator
extends the map by editing this file.

Mirror in shape of ``intel_gates._COINGECKO_ID_MAP`` so the two stay
loosely related — an operator who adds a coin to one usually wants the
other too.
"""
from __future__ import annotations

import re

# canonical lowercase name -> uppercase trading symbol (without the /USD suffix)
NAME_TO_SYMBOL: dict[str, str] = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "cardano": "ADA",
    "ripple": "XRP",
    "xrp": "XRP",
    "dogecoin": "DOGE",
    "avalanche": "AVAX",
    "chainlink": "LINK",
    "polkadot": "DOT",
    "litecoin": "LTC",
    "bitcoin cash": "BCH",
    "uniswap": "UNI",
    "arbitrum": "ARB",
    "filecoin": "FIL",
    "aave": "AAVE",
    "curve": "CRV",
    "the graph": "GRT",
    "polygon": "POL",
    "matic": "POL",
    "cosmos": "ATOM",
    "near protocol": "NEAR",
    "near": "NEAR",
    "sui": "SUI",
    "aptos": "APT",
    "stellar": "XLM",
    "tron": "TRX",
    "shiba inu": "SHIB",
    "pepe": "PEPE",
    "tezos": "XTZ",
    "algorand": "ALGO",
    "hedera": "HBAR",
    "vechain": "VET",
    "render": "RNDR",
    "injective": "INJ",
    "optimism": "OP",
    "bonk": "BONK",
    "dogwifhat": "WIF",
    "fetch.ai": "FET",
    "fetch": "FET",
}


# Pre-compile a single regex: longest names first so "bitcoin cash" matches
# before "bitcoin". Word boundaries prevent "bitcoin" matching inside
# "bitcoinmagazine".
_NAMES_BY_LENGTH = sorted(NAME_TO_SYMBOL.keys(), key=len, reverse=True)
_NAME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _NAMES_BY_LENGTH) + r")\b",
    re.IGNORECASE,
)


def extract_symbols_from_text(text: str) -> list[str]:
    """Return deduped uppercase trading symbols mentioned in ``text``.

    Matches canonical names from ``NAME_TO_SYMBOL`` (case-insensitive,
    word-bounded) and also explicit ``$TICKER`` patterns. Unknown names
    are dropped — we don't fuzzy-match.
    """
    if not text:
        return []
    out: set[str] = set()
    for m in _NAME_PATTERN.finditer(text):
        name = m.group(1).lower()
        sym = NAME_TO_SYMBOL.get(name)
        if sym:
            out.add(sym)
    # Also pick up explicit $TICKER mentions for crypto symbols already
    # in our map (avoids false positives from equity tickers in mixed feeds).
    known_symbols = set(NAME_TO_SYMBOL.values())
    for m in re.finditer(r"\$([A-Z]{2,6})\b", text):
        sym = m.group(1).upper()
        if sym in known_symbols:
            out.add(sym)
    return sorted(out)
