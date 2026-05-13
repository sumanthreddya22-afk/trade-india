"""Position classification for v4 Phase 0.

Every position the bot manages must classify as one of:

  - ``bot``       : opened via a bot-generated client_order_id pattern
  - ``external``  : present on the broker with no matching bot order
  - ``manual``    : opened via a CLI manual command (origin == "manual")
  - ``unknown``   : neither rule matched

In v4 the risk kernel (Phase 2) halts new entries while any open
position classifies as ``unknown`` for more than 15 minutes (Section 6
kill switches). Phase 0 only ships the classifier and surfaces the
result; the runtime halt lands when the risk kernel does.

Bot client_order_id pattern (v4 Section 5, ledger schema):

    YYYYMMDD_<strategy>_<symbol>_<seq>

For historical positions opened before this convention was enforced, the
``bot_client_order_id_prefixes`` list lets the caller pass a set of
known-bot prefixes (e.g. ``["trading-bot-", "wheel-runner-"]``) so the
backfill recognises them as bot-originated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Optional, Sequence

ClassificationT = Literal["bot", "external", "manual", "unknown"]

# v4 client_order_id shape: ``YYYYMMDD_<strategy>_<symbol>_<seq>``.
# The strategy segment may contain underscores (e.g. ``ETF_MOMENTUM``).
# Anchor on the date prefix and the trailing numeric seq.
_BOT_CLIENT_ORDER_ID = re.compile(
    r"^\d{8}_[A-Za-z0-9_\-]+_[A-Z0-9\-/]+_\d+$"
)

DEFAULT_LEGACY_PREFIXES: tuple[str, ...] = (
    "trading-bot-",
    "wheel-",
    "scout-",
    "stocks-",
    "crypto-",
    "options-",
)


@dataclass(frozen=True)
class BrokerPosition:
    """Minimal shape needed for classification — keeps the function
    decoupled from the Alpaca SDK so tests can pass plain dataclasses.
    """

    symbol: str
    asset_class: str = ""
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    origin: Optional[str] = None  # "strategy" | "manual" | None


@dataclass(frozen=True)
class OrderMasterRow:
    """Tiny stand-in for the Phase 1 ledger ``order_master`` row."""

    client_order_id: str
    origin: str
    symbol: str


OrderLookupT = Callable[[str], Optional[OrderMasterRow]]


def looks_like_bot_client_order_id(
    cid: str | None,
    *,
    legacy_prefixes: Sequence[str] = DEFAULT_LEGACY_PREFIXES,
) -> bool:
    if not cid:
        return False
    if _BOT_CLIENT_ORDER_ID.match(cid):
        return True
    return any(cid.startswith(p) for p in legacy_prefixes)


def classify(
    position: BrokerPosition,
    order_master_lookup: OrderLookupT | None = None,
    *,
    legacy_prefixes: Sequence[str] = DEFAULT_LEGACY_PREFIXES,
) -> ClassificationT:
    """Classify a single position.

    Resolution order (first match wins):

      1. ``position.origin == "manual"``                     → ``manual``
      2. order_master row exists for the client_order_id     → ``bot``
         (only when origin in row is "strategy")
      3. client_order_id matches a bot pattern or prefix     → ``bot``
      4. client_order_id present but no order_master match   → ``external``
      5. no client_order_id at all                           → ``unknown``

    Until Phase 1's ledger ships, ``order_master_lookup`` is None and
    rule 2 is skipped — that's fine because rules 3/4/5 already cover
    every observable case.
    """
    origin = (position.origin or "").lower()
    if origin == "manual":
        return "manual"

    cid = position.client_order_id

    if order_master_lookup is not None and cid:
        row = order_master_lookup(cid)
        if row is not None:
            row_origin = (row.origin or "").lower()
            if row_origin == "manual":
                return "manual"
            if row_origin == "strategy":
                return "bot"
            return "unknown"

    if cid is None:
        return "unknown"

    if looks_like_bot_client_order_id(cid, legacy_prefixes=legacy_prefixes):
        return "bot"

    # Has an id but doesn't look like ours and we have no ledger row to
    # confirm it — treat as external (broker- or user-originated).
    return "external"


def classify_many(
    positions: Iterable[BrokerPosition],
    order_master_lookup: OrderLookupT | None = None,
    *,
    legacy_prefixes: Sequence[str] = DEFAULT_LEGACY_PREFIXES,
) -> list[tuple[BrokerPosition, ClassificationT]]:
    """Convenience: classify a batch. Preserves input order."""
    return [
        (p, classify(p, order_master_lookup, legacy_prefixes=legacy_prefixes))
        for p in positions
    ]


__all__ = [
    "BrokerPosition",
    "ClassificationT",
    "DEFAULT_LEGACY_PREFIXES",
    "OrderLookupT",
    "OrderMasterRow",
    "classify",
    "classify_many",
    "looks_like_bot_client_order_id",
]
