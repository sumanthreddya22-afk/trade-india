"""WS5d — 4 P&L tripwires.

| # | Tripwire     | Measures                                       | Alert    | Halt     | Window           |
|---|--------------|------------------------------------------------|----------|----------|------------------|
| 1 | realized_loss| Realized P&L (excludes mark-to-market)         | 2% / $20 | 3% / $30 | 24h rolling      |
| 2 | drift        | Live-vs-model P&L divergence                   | 1.2x     | 1.5x     | 20-trade rolling |
| 3 | exec_quality | Slippage vs cost model prediction              | >10 bps  | >15 bps  | 10-fill rolling  |
| 4 | behavioural  | Trade-count / position-count anomalies         | mismatch | extreme  | per cron tick    |

Each evaluator returns a ``TripwireFinding`` with the observed value,
the threshold it crossed, and a free-form ``reason``. Callers in
``daemon/jobs.py`` write the finding through
``ledger.alert_event.write_event``; ``halt`` severity additionally
fires a kill switch via ``risk.kill_switches.write_kill``.

All evaluators are pure functions on summary inputs so they're
trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


# Thresholds — kept in code (not policy lock) because they're cockpit
# parameters, not promotion gates. The plan calls these "tripwires", not
# validation policy.
REALIZED_LOSS_ALERT_PCT = 2.0
REALIZED_LOSS_HALT_PCT = 3.0
REALIZED_LOSS_ALERT_USD = 20.0
REALIZED_LOSS_HALT_USD = 30.0
REALIZED_LOSS_WINDOW = "24h"

DRIFT_ALERT_MULTIPLIER = 1.2
DRIFT_HALT_MULTIPLIER = 1.5
DRIFT_WINDOW = "20_trade_rolling"

EXEC_QUALITY_ALERT_BPS = 10.0
EXEC_QUALITY_HALT_BPS = 15.0
EXEC_QUALITY_WINDOW = "10_fill_rolling"

BEHAV_EXTREME_TRADE_MULTIPLE = 3.0       # 3x expected trade count
BEHAV_POSITION_OVERAGE = 2                # expected+2 positions
BEHAV_WINDOW = "per_cron_tick"


@dataclass(frozen=True)
class TripwireFinding:
    tripwire: str           # realized_loss | drift | exec_quality | behavioural
    severity: str           # alert | halt | None (None = no breach)
    observed: float
    threshold: float
    window: str
    reason: str


def evaluate_realized_loss(
    *, realized_loss_usd: float, equity_usd: float,
) -> Optional[TripwireFinding]:
    """Tripwire 1 — realized losses over the last 24h.

    Two thresholds in OR: either the percent-of-equity OR the absolute
    dollar floor. The dollar floor catches $1k-account scenarios where
    2% = $20 is the meaningful level even when pct check would defer.
    """
    if realized_loss_usd >= 0:
        return None
    loss_usd = -realized_loss_usd
    loss_pct = (loss_usd / equity_usd * 100.0) if equity_usd > 0 else 0.0
    if (loss_pct >= REALIZED_LOSS_HALT_PCT
            or loss_usd >= REALIZED_LOSS_HALT_USD):
        return TripwireFinding(
            tripwire="realized_loss", severity="halt",
            observed=loss_pct, threshold=REALIZED_LOSS_HALT_PCT,
            window=REALIZED_LOSS_WINDOW,
            reason=(
                f"realized_loss ${loss_usd:.2f} ({loss_pct:.2f}% of equity) "
                f">= halt threshold {REALIZED_LOSS_HALT_PCT}% / "
                f"${REALIZED_LOSS_HALT_USD}"
            ),
        )
    if (loss_pct >= REALIZED_LOSS_ALERT_PCT
            or loss_usd >= REALIZED_LOSS_ALERT_USD):
        return TripwireFinding(
            tripwire="realized_loss", severity="alert",
            observed=loss_pct, threshold=REALIZED_LOSS_ALERT_PCT,
            window=REALIZED_LOSS_WINDOW,
            reason=(
                f"realized_loss ${loss_usd:.2f} ({loss_pct:.2f}% of equity) "
                f">= alert threshold {REALIZED_LOSS_ALERT_PCT}% / "
                f"${REALIZED_LOSS_ALERT_USD}"
            ),
        )
    return None


def evaluate_drift(
    *, realised_mean_bps: float, modelled_mean_bps: float,
) -> Optional[TripwireFinding]:
    """Tripwire 2 — live-vs-model drift. Wraps the drift_monitor ratio
    but exposes the two-tier (alert/halt) severity that the plan calls
    for. Note: the existing TOLERANCE_MULTIPLIER_DEFAULT in
    drift_monitor.py (1.5 post-WS4) matches our halt threshold here.
    """
    if modelled_mean_bps <= 0:
        return None
    ratio = realised_mean_bps / modelled_mean_bps
    if ratio >= DRIFT_HALT_MULTIPLIER:
        return TripwireFinding(
            tripwire="drift", severity="halt",
            observed=ratio, threshold=DRIFT_HALT_MULTIPLIER,
            window=DRIFT_WINDOW,
            reason=(
                f"realised/modelled slippage ratio={ratio:.2f} "
                f">= halt threshold {DRIFT_HALT_MULTIPLIER}"
            ),
        )
    if ratio >= DRIFT_ALERT_MULTIPLIER:
        return TripwireFinding(
            tripwire="drift", severity="alert",
            observed=ratio, threshold=DRIFT_ALERT_MULTIPLIER,
            window=DRIFT_WINDOW,
            reason=(
                f"realised/modelled slippage ratio={ratio:.2f} "
                f">= alert threshold {DRIFT_ALERT_MULTIPLIER}"
            ),
        )
    return None


def evaluate_exec_quality(
    *, recent_slippages_bps: Sequence[float],
) -> Optional[TripwireFinding]:
    """Tripwire 3 — execution-quality. Mean absolute slippage over the
    last 10 fills."""
    if not recent_slippages_bps:
        return None
    sample = list(recent_slippages_bps)[-10:]
    if not sample:
        return None
    mean_abs = sum(abs(s) for s in sample) / len(sample)
    if mean_abs >= EXEC_QUALITY_HALT_BPS:
        return TripwireFinding(
            tripwire="exec_quality", severity="halt",
            observed=mean_abs, threshold=EXEC_QUALITY_HALT_BPS,
            window=EXEC_QUALITY_WINDOW,
            reason=(
                f"mean abs slippage over last {len(sample)} fills = "
                f"{mean_abs:.1f}bps >= halt {EXEC_QUALITY_HALT_BPS}bps"
            ),
        )
    if mean_abs >= EXEC_QUALITY_ALERT_BPS:
        return TripwireFinding(
            tripwire="exec_quality", severity="alert",
            observed=mean_abs, threshold=EXEC_QUALITY_ALERT_BPS,
            window=EXEC_QUALITY_WINDOW,
            reason=(
                f"mean abs slippage over last {len(sample)} fills = "
                f"{mean_abs:.1f}bps >= alert {EXEC_QUALITY_ALERT_BPS}bps"
            ),
        )
    return None


def evaluate_behavioural(
    *,
    observed_trade_count: int,
    expected_trade_count: int,
    observed_position_count: int,
    expected_position_count: int,
) -> Optional[TripwireFinding]:
    """Tripwire 4 — anomalous trade or position counts."""
    # trades=0 when we expected some -> alert
    if expected_trade_count > 0 and observed_trade_count == 0:
        return TripwireFinding(
            tripwire="behavioural", severity="alert",
            observed=float(observed_trade_count),
            threshold=float(expected_trade_count),
            window=BEHAV_WINDOW,
            reason=(
                f"expected {expected_trade_count} trades, observed 0 "
                f"(scheduler may be stuck)"
            ),
        )
    # trades >> expected -> halt
    if (expected_trade_count > 0
            and observed_trade_count
            >= expected_trade_count * BEHAV_EXTREME_TRADE_MULTIPLE):
        return TripwireFinding(
            tripwire="behavioural", severity="halt",
            observed=float(observed_trade_count),
            threshold=expected_trade_count * BEHAV_EXTREME_TRADE_MULTIPLE,
            window=BEHAV_WINDOW,
            reason=(
                f"observed {observed_trade_count} trades vs expected "
                f"{expected_trade_count} (>= {BEHAV_EXTREME_TRADE_MULTIPLE}x)"
            ),
        )
    # positions > expected + 2 -> halt
    if observed_position_count > expected_position_count + BEHAV_POSITION_OVERAGE:
        return TripwireFinding(
            tripwire="behavioural", severity="halt",
            observed=float(observed_position_count),
            threshold=float(expected_position_count + BEHAV_POSITION_OVERAGE),
            window=BEHAV_WINDOW,
            reason=(
                f"observed {observed_position_count} positions vs expected "
                f"{expected_position_count} + {BEHAV_POSITION_OVERAGE}"
            ),
        )
    return None


__all__ = [
    "TripwireFinding",
    "evaluate_behavioural",
    "evaluate_drift",
    "evaluate_exec_quality",
    "evaluate_realized_loss",
]
