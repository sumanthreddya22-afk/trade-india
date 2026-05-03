"""Crypto adversarial defense (Phase 1F.2).

Pure-function flag computation. The aggregator calls
``compute_flags(symbol, events, context)`` once per candidate at
roll-up time and writes the resulting flags to
``intel_candidates_crypto`` so the scout debate brief can reference
them.

Five crypto-specific signals (additive to the shared URL-dedup +
velocity + coordination + pump heuristics on the stocks side):

  cold_start_token       — token age < 30 days
  whale_concentration    — top-10 holder concentration > 50%
  honeypot_detected      — contract not verified on-chain AND owner-
                           can-mint privilege detected
  sybil_coordinated      — > 50 Twitter / social accounts < 30 days
                           old promoting the same token within a 1-hour
                           window
  pump_signature         — listing-age < 24h AND volume spike > 5×
                           baseline (replaces the equity small-cap rule)

All inputs are passed in (token age, holder distribution, contract
metadata, sybil-account scan output, listing-age, volume spike). The
module does not query external APIs itself — that's the data layer's
job (``intel/sources/``). This keeps the flag computation pure and
trivially testable.

When a hard signal fires (honeypot), the candidate's score multiplier
is forced to 0.0 — overrides everything else. Other flags multiply the
score by their attenuation (cold-start = 0.5×, etc.).

The score-multiplier output is consumed by the aggregator at upsert time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default thresholds (mirrors strategy/config.yaml `crypto_adversarial:`)
# ---------------------------------------------------------------------------


@dataclass
class CryptoAdversarialThresholds:
    cold_start_token_age_days: int = 30
    cold_start_score_multiplier: float = 0.5
    whale_concentration_top10_pct: float = 50.0
    honeypot_score_multiplier: float = 0.0      # hard-zero
    sybil_account_count: int = 50
    sybil_account_max_age_days: int = 30
    sybil_window_minutes: int = 60
    pump_listing_age_hours: int = 24
    pump_volume_spike_ratio: float = 5.0
    pump_score_multiplier: float = 0.5


# ---------------------------------------------------------------------------
# Input shape — caller assembles this from upstream data sources
# ---------------------------------------------------------------------------


@dataclass
class AdversarialContext:
    """All the per-candidate signals needed to compute the 5 flags.

    Most fields are optional — when a signal isn't available, that
    flag silently returns False (graceful degradation). For example,
    if Etherscan is rate-limited and we can't get holder distribution,
    the whale_concentration check is simply skipped.
    """
    symbol: str
    chain: Optional[str] = None
    # Cold-start
    token_age_days: Optional[int] = None
    # Whale concentration
    top_10_holder_pct: Optional[float] = None
    # Honeypot
    contract_verified: Optional[bool] = None
    owner_can_mint: Optional[bool] = None
    # Sybil
    young_promotional_accounts_count: Optional[int] = None
    young_promotional_window_minutes: Optional[int] = None
    # Pump
    listing_age_hours: Optional[float] = None
    volume_spike_ratio: Optional[float] = None


# ---------------------------------------------------------------------------
# Flag-computation result
# ---------------------------------------------------------------------------


@dataclass
class AdversarialFlags:
    cold_start_token: bool = False
    whale_concentration: bool = False
    honeypot_detected: bool = False
    sybil_coordinated: bool = False
    pump_signature: bool = False
    score_multiplier: float = 1.0
    detail: Dict[str, str] = field(default_factory=dict)

    @property
    def any_flag_set(self) -> bool:
        return (
            self.cold_start_token
            or self.whale_concentration
            or self.honeypot_detected
            or self.sybil_coordinated
            or self.pump_signature
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cold_start_token":     self.cold_start_token,
            "whale_concentration":  self.whale_concentration,
            "honeypot_detected":    self.honeypot_detected,
            "sybil_coordinated":    self.sybil_coordinated,
            "pump_signature":       self.pump_signature,
            "score_multiplier":     self.score_multiplier,
            "detail":               dict(self.detail),
        }


# ---------------------------------------------------------------------------
# Pure flag computers — one per signal, each fully testable
# ---------------------------------------------------------------------------


def _check_cold_start(
    ctx: AdversarialContext, th: CryptoAdversarialThresholds,
) -> tuple[bool, str]:
    if ctx.token_age_days is None:
        return False, ""
    if ctx.token_age_days < th.cold_start_token_age_days:
        return True, f"token_age_days={ctx.token_age_days} < {th.cold_start_token_age_days}"
    return False, ""


def _check_whale_concentration(
    ctx: AdversarialContext, th: CryptoAdversarialThresholds,
) -> tuple[bool, str]:
    if ctx.top_10_holder_pct is None:
        return False, ""
    if ctx.top_10_holder_pct >= th.whale_concentration_top10_pct:
        return True, f"top_10_holder_pct={ctx.top_10_holder_pct:.1f} >= {th.whale_concentration_top10_pct}"
    return False, ""


def _check_honeypot(
    ctx: AdversarialContext, th: CryptoAdversarialThresholds,
) -> tuple[bool, str]:
    """Honeypot signature requires BOTH unverified contract AND owner-mint privilege.
    Either signal alone is informative but not actionable.
    """
    if ctx.contract_verified is None or ctx.owner_can_mint is None:
        return False, ""
    if (not ctx.contract_verified) and ctx.owner_can_mint:
        return True, "contract not verified AND owner can mint"
    return False, ""


def _check_sybil(
    ctx: AdversarialContext, th: CryptoAdversarialThresholds,
) -> tuple[bool, str]:
    if (ctx.young_promotional_accounts_count is None
        or ctx.young_promotional_window_minutes is None):
        return False, ""
    if (ctx.young_promotional_accounts_count >= th.sybil_account_count
        and ctx.young_promotional_window_minutes <= th.sybil_window_minutes):
        return (
            True,
            f"{ctx.young_promotional_accounts_count} young accounts in "
            f"{ctx.young_promotional_window_minutes}min window "
            f"(threshold: {th.sybil_account_count} in {th.sybil_window_minutes}min)",
        )
    return False, ""


def _check_pump_signature(
    ctx: AdversarialContext, th: CryptoAdversarialThresholds,
) -> tuple[bool, str]:
    """Crypto pump signature: just-listed (< 24h) AND volume spike > 5×.
    Replaces the equity-side small-cap rule (which doesn't apply to crypto).
    """
    if ctx.listing_age_hours is None or ctx.volume_spike_ratio is None:
        return False, ""
    if (ctx.listing_age_hours < th.pump_listing_age_hours
        and ctx.volume_spike_ratio > th.pump_volume_spike_ratio):
        return (
            True,
            f"listing_age={ctx.listing_age_hours:.1f}h AND volume_spike="
            f"{ctx.volume_spike_ratio:.1f}x > {th.pump_volume_spike_ratio}",
        )
    return False, ""


# ---------------------------------------------------------------------------
# Aggregator entry point
# ---------------------------------------------------------------------------


def compute_flags(
    ctx: AdversarialContext,
    *,
    thresholds: Optional[CryptoAdversarialThresholds] = None,
) -> AdversarialFlags:
    """Run all 5 crypto adversarial checks and combine into ``AdversarialFlags``.

    Score multiplier composition:
      - honeypot ⇒ 0.0 (hard, overrides everything)
      - else: cold_start (0.5×) AND pump (0.5×) MULTIPLY together → 0.25×
        when both fire. whale_concentration / sybil flag the candidate
        for the scout brief but do NOT multiplicatively reduce score
        (the judge weighs them with full context).
    """
    th = thresholds or CryptoAdversarialThresholds()
    flags = AdversarialFlags()

    flags.cold_start_token, d = _check_cold_start(ctx, th)
    if d: flags.detail["cold_start_token"] = d

    flags.whale_concentration, d = _check_whale_concentration(ctx, th)
    if d: flags.detail["whale_concentration"] = d

    flags.honeypot_detected, d = _check_honeypot(ctx, th)
    if d: flags.detail["honeypot_detected"] = d

    flags.sybil_coordinated, d = _check_sybil(ctx, th)
    if d: flags.detail["sybil_coordinated"] = d

    flags.pump_signature, d = _check_pump_signature(ctx, th)
    if d: flags.detail["pump_signature"] = d

    if flags.honeypot_detected:
        flags.score_multiplier = th.honeypot_score_multiplier
    else:
        mult = 1.0
        if flags.cold_start_token:
            mult *= th.cold_start_score_multiplier
        if flags.pump_signature:
            mult *= th.pump_score_multiplier
        flags.score_multiplier = mult

    return flags
