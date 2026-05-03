"""Snapshot.org DAO-vote collector.

Off-chain governance proposals for major DeFi protocols (UNI, AAVE,
MKR, COMP, ENS, etc.) live on Snapshot.org. Material tokenomics
changes that pass on-chain are deterministic catalysts.

Free GraphQL endpoint at https://hub.snapshot.org/graphql.

Sentiment heuristic:
    proposal CLOSED + state == passed   →  +0.3   (executed change)
    proposal CLOSED + state == rejected →  -0.2   (continuity preserved)
    proposal ACTIVE                     →   0.0   (informational only)
The body of the proposal usually controls the directional bias; we
keep the sentiment small and let the LLM debate read the headline.

Each tracked space (e.g. "uniswap") maps 1:1 to a token symbol via
SPACE_TO_SYMBOL — Phase 1D will populate this from per-chain
attribution data.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

from trading_bot.pipelines.crypto.sources._base import (
    SourceResult,
    normalize_crypto_symbol,
    stable_event_hash,
    utcnow,
    write_event,
)

logger = logging.getLogger(__name__)

SNAPSHOT_GRAPHQL_URL = "https://hub.snapshot.org/graphql"

# Snapshot space slug → governance token. Curated; extend as needed.
SPACE_TO_SYMBOL: Dict[str, str] = {
    "uniswap":   "UNI",
    "aave.eth":  "AAVE",
    "compound-finance.eth": "COMP",
    "makerdao.eth": "MKR",
    "ens.eth":   "ENS",
    "balancer.eth": "BAL",
    "curve.eth":    "CRV",
    "lido-snapshot.eth": "LDO",
    "snshot.eth": "SNX",
    "1inch.eth": "1INCH",
}


def _classify_state(state: str) -> float:
    s = (state or "").lower()
    if s == "closed":
        return 0.3
    if s == "rejected":
        return -0.2
    return 0.0


def collect_snapshot_governance(
    engine: Any,
    *,
    settings: Any = None,
    spaces: Optional[Iterable[str]] = None,
    proposals_per_space: int = 5,
    fetcher: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    now: Optional[dt.datetime] = None,
) -> SourceResult:
    """Pull recent proposals for each tracked space; write one event per proposal."""
    now = now or utcnow()
    targets = list(spaces) if spaces is not None else list(SPACE_TO_SYMBOL.keys())
    if not targets:
        return SourceResult(source="snapshot_governance", extra={"reason": "no spaces configured"})

    written = 0
    skipped = 0
    errors: List[str] = []

    for space in targets:
        symbol_token = SPACE_TO_SYMBOL.get(space)
        if not symbol_token:
            skipped += 1
            continue
        try:
            proposals = (fetcher or _default_fetcher)(space, proposals_per_space) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("snapshot_governance %s fetch failed: %s", space, e)
            errors.append(f"{space}:{e}")
            continue

        for prop in proposals:
            try:
                w, s = _write_one_proposal(engine, prop, space=space,
                                           symbol_token=symbol_token, now=now)
                written += w
                skipped += s
            except Exception as e:  # noqa: BLE001
                logger.warning("snapshot_governance: skipped malformed proposal: %s", e)
                skipped += 1

    return SourceResult(
        source="snapshot_governance",
        written=written,
        skipped=skipped,
        error="; ".join(errors) if errors else None,
        extra={"spaces_polled": len(targets)},
    )


def _write_one_proposal(
    engine: Any,
    prop: Dict[str, Any],
    *,
    space: str,
    symbol_token: str,
    now: dt.datetime,
) -> tuple[int, int]:
    pid = prop.get("id") or ""
    title = (prop.get("title") or "").strip()
    state = prop.get("state") or ""
    if not pid or not title:
        return (0, 1)

    end_ts = prop.get("end")
    event_at: Optional[dt.datetime] = None
    if end_ts:
        try:
            event_at = dt.datetime.fromtimestamp(int(end_ts), tz=dt.timezone.utc)
        except (TypeError, ValueError):
            event_at = None

    sentiment = _classify_state(state)
    headline = f"[{space}] {title} (state={state})"[:1000]
    url = f"https://snapshot.org/#/{space}/proposal/{pid}"

    ok = write_event(
        engine,
        symbol=normalize_crypto_symbol(symbol_token),
        source="snapshot_governance",
        headline=headline,
        url=url,
        sentiment=sentiment,
        event_at=event_at,
        event_hash=stable_event_hash("snapshot_governance", space, pid, state),
        now=now,
    )
    return (1, 0) if ok else (0, 1)


def _default_fetcher(space: str, limit: int) -> List[Dict[str, Any]]:
    """Live GraphQL call to Snapshot.org. Mocked in tests."""
    import requests

    query = """
    query Proposals($space: String!, $first: Int!) {
      proposals(first: $first, where: {space: $space},
                orderBy: "created", orderDirection: desc) {
        id title state end
      }
    }
    """
    resp = requests.post(
        SNAPSHOT_GRAPHQL_URL,
        json={"query": query, "variables": {"space": space, "first": limit}},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    return ((payload.get("data") or {}).get("proposals") or [])
