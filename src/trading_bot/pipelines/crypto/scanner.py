"""Crypto pipeline scanner — closes the Phase 1G.3 bypass.

Until this module shipped, ``roles/crypto_scanner.py`` ran
``cli.crypto_scan`` which drew its watchlist from the manual Alpaca
crypto-universe file — meaning scout-debate verdicts (Phase 1B) never
flowed into actual order placement.

This module provides the missing link:

  ``load_elevated_watchlist(engine)``  →  ``list[WatchlistEntry]``
      Reads ``intel_candidates_crypto`` filtered to ``scout_verdict
      == 'elevate'`` AND not currently dismissed. Caller fills the
      orchestrator scan from this list, so only scout-elevated
      candidates become trade decisions.

  ``run_crypto_scan_with_scout(...)``
      Orchestrator-driven scan that uses the elevated watchlist and
      transparently falls back to the manual universe when scout has
      not yet produced any elevated candidates (cold start, post-rate-
      limit recovery, etc.).

Per Option 2: this module reads only from crypto-owned tables and
imports nothing from the stocks pipeline.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.state_db import IntelCandidateCrypto
from trading_bot.state import WatchlistEntry

logger = logging.getLogger(__name__)


DEFAULT_MAX_SYMBOLS = 25


def load_elevated_watchlist(
    engine: Any,
    *,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    now: Optional[dt.datetime] = None,
) -> List[WatchlistEntry]:
    """Return scout-elevated crypto candidates as a watchlist.

    Filter:
      - ``scout_verdict == 'elevate'``
      - ``scout_dismissed_until IS NULL OR scout_dismissed_until <= now``
        (a verdict can carry both, but elevate clears dismissed_until
        in scout_debate.apply_verdicts; this safety check protects
        against schema drift)
    Order: highest ``score`` first.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as session:
        rows = (
            session.query(IntelCandidateCrypto)
            .filter(IntelCandidateCrypto.scout_verdict == "elevate")
            .filter(
                (IntelCandidateCrypto.scout_dismissed_until.is_(None))
                | (IntelCandidateCrypto.scout_dismissed_until <= now)
            )
            .order_by(IntelCandidateCrypto.score.desc())
            .limit(max_symbols)
            .all()
        )

    return [
        WatchlistEntry(
            symbol=r.symbol,
            asset_class="crypto",
            notes=f"scout_elevate score={r.score:.2f} top={(r.top_reason or '')[:80]}",
        )
        for r in rows
    ]


def run_crypto_scan_with_scout(
    *,
    settings: Any,
    cfg: Any,
    alpaca: Any,
    market: Any,
    journal: Any,
    state_engine: Any,
    fallback_to_universe: bool = True,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    pnl_state_builder: Optional[Any] = None,
    now: Optional[dt.datetime] = None,
) -> Any:
    """Orchestrator-driven crypto scan that prefers scout-elevated candidates.

    Drop-in replacement for the body of ``cli.crypto_scan``. Caller
    constructs the orchestrator's deps (alpaca, market, journal,
    pnl_state) and passes them through.

    Behaviour:
      - Read elevated watchlist from intel_candidates_crypto.
      - If empty AND ``fallback_to_universe``: log + delegate to
        ``cli._load_active_universe(crypto_only=True)`` so the bot
        still trades on cold-start days.
      - Run ``orchestrator.scan(watchlist=...)`` with whichever list
        we got. The orchestrator already applies risk, compliance,
        crypto sentiment gates, entry-debate, and order placement —
        we just feed it a scout-curated watchlist.

    Returns the ``ScanResult`` from the orchestrator.
    """
    # Local imports to avoid pulling daemon-time deps into module init.
    from trading_bot.cli import _build_orchestrator, _load_active_universe, _live_regime
    from trading_bot.pnl_state import PnlStateBuilder

    elevated = load_elevated_watchlist(state_engine, max_symbols=max_symbols, now=now)
    if elevated:
        watchlist = elevated
        source = "scout_elevated"
    elif fallback_to_universe:
        logger.info("crypto_scan_with_scout: no elevated candidates; falling back to manual universe")
        watchlist = _load_active_universe(crypto_only=True)
        source = "manual_universe_fallback"
    else:
        logger.info("crypto_scan_with_scout: no elevated candidates; not falling back")
        return None

    if not watchlist:
        logger.info("crypto_scan_with_scout: empty watchlist (source=%s)", source)
        return None

    builder = pnl_state_builder or PnlStateBuilder(settings, cfg)
    regime_reading = _live_regime(market, cfg)
    orch = _build_orchestrator(
        cfg=cfg, market=market, alpaca=alpaca, journal=journal,
        regime=regime_reading.regime.value, settings=settings,
        state_builder=builder.to_risk_state,
    )
    result = orch.scan(watchlist=watchlist)
    logger.info(
        "crypto_scan_with_scout: source=%s symbols=%d placed=%d rejected=%d",
        source, len(watchlist),
        sum(1 for d in result.decisions if d.action == "placed_order"),
        sum(1 for d in result.decisions if d.action == "rejected_by_risk"),
    )
    return result
