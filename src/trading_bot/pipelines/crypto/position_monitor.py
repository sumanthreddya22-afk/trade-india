"""Crypto position monitor (Phase 1C).

Reads currently-held crypto positions from the broker, classifies each
through the crypto-specific trigger set, and dispatches the hold
debate when at least one trigger fires.

Trigger set (crypto-only — additive to the shared score-drop / sentiment-
flip triggers from the stocks pipeline):

  funding_extreme      — Binance perp funding > 0.15%/8h on the held perp
  chain_exploit        — fresh rekt_exploit event on the same chain or
                         a related-protocol of the held position
  stablecoin_depeg     — USDT or USDC > 1.5% off peg AND held position
                         has stablecoin counterparty exposure
  whale_outflow        — fresh whale_alert event for the held symbol with
                         direction = exchange-inbound (sell pressure) and
                         amount > $1M

Each trigger reads ONLY from crypto-owned tables (intel_events_crypto)
plus a small price-feed callable (for the depeg check). No imports from
the stocks pipeline.

Failure mode: any single trigger blowing up doesn't mask the others.
The classifier traps per-trigger exceptions and keeps walking. If the
hold-debate LLM call rate-limits, the position monitor reports it but
does NOT take action — brackets stay live.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from trading_bot.pipelines.crypto.hold_debate import (
    HoldActionExecutor,
    HoldRunResult,
    TriggerContext,
    run_hold_debate,
)
from trading_bot.pipelines.crypto.state_db import IntelEventCrypto

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurable thresholds (mirrors strategy/config.yaml `crypto_hold:` block)
# ---------------------------------------------------------------------------


@dataclass
class HoldThresholds:
    funding_extreme_threshold: float = 0.0015      # 0.15%/8h
    whale_outflow_min_usd: float = 1_000_000
    stablecoin_depeg_pct: float = 1.5              # absolute % off $1.00 peg
    chain_exploit_lookback_hours: int = 12
    funding_lookback_hours: int = 4
    whale_outflow_lookback_hours: int = 4
    related_protocols: Dict[str, List[str]] = field(default_factory=lambda: {
        # held_chain → list of protocols that are upstream/related
        "ethereum": ["arbitrum", "optimism", "base", "ethereum"],
        "arbitrum": ["arbitrum", "ethereum"],
        "optimism": ["optimism", "ethereum"],
        "base":     ["base", "ethereum"],
        "solana":   ["solana"],
        "bsc":      ["bsc"],
    })


# ---------------------------------------------------------------------------
# Held-position abstraction (broker-agnostic)
# ---------------------------------------------------------------------------


@dataclass
class HeldCryptoPosition:
    symbol: str                          # e.g. "ETH/USD"
    entry_order_id: Optional[str] = None
    side: str = "long"
    qty: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    pnl_pct: float = 0.0
    days_held: float = 0.0
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    chain: Optional[str] = None          # e.g. "ethereum" — set from intel snapshot
    has_stablecoin_exposure: bool = False  # True for any USDT/USDC/etc-quoted pair
    entry_score: Optional[float] = None
    current_score: Optional[float] = None
    entry_sentiment: Optional[float] = None
    current_sentiment: Optional[float] = None


# ---------------------------------------------------------------------------
# Trigger classification (per position)
# ---------------------------------------------------------------------------


@dataclass
class _TriggerHit:
    name: str
    evidence: str


def _strip_quote(symbol: str) -> str:
    """``BTC/USD`` → ``BTC`` for matching against funding / depeg signals."""
    return symbol.split("/", 1)[0].upper()


def _check_funding_extreme(
    engine: Any, position: HeldCryptoPosition, *,
    cutoff: dt.datetime, threshold: float,
) -> Optional[_TriggerHit]:
    """Look for a recent binance_funding event for THIS perp with |rate| >= threshold."""
    base = _strip_quote(position.symbol)
    binance_perp = f"{base}USDT"  # binance_funding writes raw_score = funding rate

    with Session(engine) as session:
        ev = (
            session.query(IntelEventCrypto)
            .filter(IntelEventCrypto.source == "binance_funding")
            .filter(IntelEventCrypto.ingested_at >= cutoff)
            .filter(IntelEventCrypto.headline.like(f"%{binance_perp}%"))
            .order_by(IntelEventCrypto.ingested_at.desc())
            .first()
        )
    if ev is None or ev.raw_score is None:
        return None
    if abs(float(ev.raw_score)) < threshold:
        return None
    return _TriggerHit(
        name="funding_extreme",
        evidence=(
            f"binance_funding on {binance_perp}: "
            f"{float(ev.raw_score) * 100:+.3f}%/8h ≥ threshold "
            f"{threshold * 100:.2f}%/8h ({ev.headline})"
        ),
    )


def _check_chain_exploit(
    engine: Any, position: HeldCryptoPosition, *,
    cutoff: dt.datetime, related_protocols: Dict[str, List[str]],
) -> Optional[_TriggerHit]:
    """Look for a recent rekt_exploit event whose chain matches the held position's chain
    OR is a related-protocol upstream of it.
    """
    if not position.chain:
        return None
    related = set(related_protocols.get(position.chain, []) + [position.chain])
    with Session(engine) as session:
        events = (
            session.query(IntelEventCrypto)
            .filter(IntelEventCrypto.source == "rekt_exploit")
            .filter(IntelEventCrypto.ingested_at >= cutoff)
            .filter(IntelEventCrypto.chain.in_(related))
            .order_by(IntelEventCrypto.ingested_at.desc())
            .all()
        )
    if not events:
        return None
    ev = events[0]
    return _TriggerHit(
        name="chain_exploit",
        evidence=(
            f"rekt_exploit on {ev.chain} (held position on {position.chain}): "
            f"{ev.headline}"
        ),
    )


def _check_stablecoin_depeg(
    engine: Any, position: HeldCryptoPosition, *,
    depeg_pct: float, peg_price_lookup: Optional[Callable[[str], Optional[float]]],
) -> Optional[_TriggerHit]:
    """Pull current USDT + USDC prices and flag if either has depegged."""
    if not position.has_stablecoin_exposure:
        return None
    if peg_price_lookup is None:
        return None

    for stable in ("USDT", "USDC"):
        try:
            price = peg_price_lookup(stable)
        except Exception as e:  # noqa: BLE001 — feed errors are non-fatal
            logger.warning("depeg lookup for %s failed: %s", stable, e)
            continue
        if price is None:
            continue
        deviation_pct = abs(price - 1.0) * 100
        if deviation_pct >= depeg_pct:
            return _TriggerHit(
                name="stablecoin_depeg",
                evidence=(
                    f"{stable} at ${price:.4f} (deviation {deviation_pct:.2f}% "
                    f"≥ threshold {depeg_pct:.2f}%); position has stablecoin exposure"
                ),
            )
    return None


def _check_whale_outflow(
    engine: Any, position: HeldCryptoPosition, *,
    cutoff: dt.datetime, min_usd: float,
) -> Optional[_TriggerHit]:
    """Look for fresh whale_alert events for the held symbol that are exchange-inbound
    (sell pressure) and >= min_usd. We use the headline + sentiment heuristic from the
    whale_alert collector: sentiment <= -0.4 ⇒ exchange-inbound (the collector classifies
    inbound as -0.5).
    """
    with Session(engine) as session:
        events = (
            session.query(IntelEventCrypto)
            .filter(IntelEventCrypto.source == "whale_alert")
            .filter(IntelEventCrypto.symbol == position.symbol.upper())
            .filter(IntelEventCrypto.ingested_at >= cutoff)
            .order_by(IntelEventCrypto.ingested_at.desc())
            .all()
        )
    matches = [
        ev for ev in events
        if (ev.sentiment is not None and ev.sentiment <= -0.4)
        and (ev.raw_score is not None and float(ev.raw_score) >= min_usd)
    ]
    if not matches:
        return None
    ev = matches[0]
    return _TriggerHit(
        name="whale_outflow",
        evidence=(
            f"whale_alert exchange-inbound for {position.symbol}: "
            f"${float(ev.raw_score) / 1_000_000:.1f}M ({ev.headline})"
        ),
    )


def classify_triggers(
    engine: Any,
    *,
    positions: Sequence[HeldCryptoPosition],
    thresholds: Optional[HoldThresholds] = None,
    peg_price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    now: Optional[dt.datetime] = None,
) -> List[TriggerContext]:
    """For each position, run all four trigger checks. If at least one
    fires, build a ``TriggerContext`` for the hold debate.

    Returns the list of TriggerContexts that should be debated. Empty list
    means no positions need attention this tick.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    th = thresholds or HoldThresholds()

    funding_cutoff = now - dt.timedelta(hours=th.funding_lookback_hours)
    exploit_cutoff = now - dt.timedelta(hours=th.chain_exploit_lookback_hours)
    whale_cutoff = now - dt.timedelta(hours=th.whale_outflow_lookback_hours)

    out: List[TriggerContext] = []
    for pos in positions:
        hits: List[_TriggerHit] = []
        for check in (
            ("funding_extreme",  lambda: _check_funding_extreme(
                engine, pos, cutoff=funding_cutoff,
                threshold=th.funding_extreme_threshold)),
            ("chain_exploit",    lambda: _check_chain_exploit(
                engine, pos, cutoff=exploit_cutoff,
                related_protocols=th.related_protocols)),
            ("stablecoin_depeg", lambda: _check_stablecoin_depeg(
                engine, pos, depeg_pct=th.stablecoin_depeg_pct,
                peg_price_lookup=peg_price_lookup)),
            ("whale_outflow",    lambda: _check_whale_outflow(
                engine, pos, cutoff=whale_cutoff,
                min_usd=th.whale_outflow_min_usd)),
        ):
            try:
                hit = check[1]()
            except Exception as e:  # noqa: BLE001 — per-trigger fail-soft
                logger.warning("trigger %s for %s failed: %s", check[0], pos.symbol, e)
                continue
            if hit is not None:
                hits.append(hit)

        if not hits:
            continue

        # Combine multiple hits into one TriggerContext — the judge reads
        # all evidence at once. Use the highest-priority trigger as the
        # ``trigger_reason`` for the audit row (chain_exploit > depeg >
        # whale_outflow > funding_extreme — most-acute first).
        priority = ("chain_exploit", "stablecoin_depeg", "whale_outflow", "funding_extreme")
        primary = sorted(hits, key=lambda h: priority.index(h.name))[0]
        evidence = "\n      ".join(h.evidence for h in hits)

        out.append(TriggerContext(
            symbol=pos.symbol,
            entry_order_id=pos.entry_order_id,
            trigger_reason=primary.name,
            trigger_evidence=evidence,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            current_price=pos.current_price,
            pnl_pct=pos.pnl_pct,
            days_held=pos.days_held,
            stop_price=pos.stop_price,
            take_profit_price=pos.take_profit_price,
            entry_score=pos.entry_score,
            current_score=pos.current_score,
            entry_sentiment=pos.entry_sentiment,
            current_sentiment=pos.current_sentiment,
            chain=pos.chain,
        ))
    return out


# ---------------------------------------------------------------------------
# Top-level role entry point
# ---------------------------------------------------------------------------


def monitor_positions(
    engine: Any,
    *,
    positions: Sequence[HeldCryptoPosition],
    executor: Optional[HoldActionExecutor] = None,
    transport: Any = None,
    thresholds: Optional[HoldThresholds] = None,
    peg_price_lookup: Optional[Callable[[str], Optional[float]]] = None,
    now: Optional[dt.datetime] = None,
) -> HoldRunResult:
    """Single monitor tick: classify triggers + dispatch hold debate (if any fired)."""
    triggers = classify_triggers(
        engine,
        positions=positions,
        thresholds=thresholds,
        peg_price_lookup=peg_price_lookup,
        now=now,
    )
    if not triggers:
        return HoldRunResult(debated=0, held=0, tightened=0, exited=0, skipped=0)

    return run_hold_debate(
        engine,
        triggers=triggers,
        executor=executor,
        transport=transport,
        now=now,
    )
