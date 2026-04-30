"""W2a — Data-quality gates for market bars.

The PDF requires every Decision to record `data_quality.{fresh, complete,
aligned, provenance_ok}`. This module supplies the deterministic checks the
orchestrator runs between the data-fetch and the strategy/risk path.

Design rules:
- Fail-closed: a missing/stale/bad source produces ``(False, reason)``.
- No DB or network access — pure functions over the DataFrame the caller
  already fetched.
- Cheap: every check is O(len(bars)).
- The orchestrator decides what to do with the verdict (typically: skip
  the symbol with action="skipped_stale_data" or "skipped_incomplete_data").
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

import pandas as pd


# Required columns for the quality check; volume is informational only.
_REQUIRED_OHLC = ("open", "high", "low", "close")


@dataclass(frozen=True)
class DataProvenance:
    """Per-fetch attribution. Keep it small — gets embedded in audit objects
    via ``data_snapshot_ids``."""

    source: str
    fetched_at: dt.datetime
    snapshot_id: str


def check_bar_freshness(
    bars: pd.DataFrame,
    *,
    asset_class: str,
    max_age_hours: float,
    now: dt.datetime | None = None,
) -> tuple[bool, str]:
    """Reject if the most recent bar is older than ``max_age_hours``.

    Returns ``(fresh, reason)``. ``fresh=True`` means the bar is acceptable
    and ``reason=""``. ``fresh=False`` returns a human-readable reason.

    The caller chooses the threshold — typical values:

    - daily bars during RTH: 48 hours
    - daily bars overnight/weekend: 96 hours (allow for non-trading days)
    - crypto bars: 2-6 hours (24/7 market)
    - intraday bars during RTH: 0.25-1 hour
    """
    if bars is None or len(bars) == 0:
        return False, "no bars available"
    last_ts = bars.index.max()
    if not isinstance(last_ts, (pd.Timestamp, dt.datetime)):
        return False, f"unrecognised index type {type(last_ts).__name__}"
    last = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    age = now_utc - last
    age_hours = age.total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return False, (
            f"stale: last bar age {age_hours:.1f}h > max {max_age_hours:.1f}h "
            f"({asset_class})"
        )
    return True, ""


def check_completeness(
    bars: pd.DataFrame,
    *,
    max_missing_pct: float = 5.0,
) -> tuple[bool, str]:
    """Reject if any required OHLC column is missing entirely or if more
    than ``max_missing_pct`` of OHLC values are NaN.

    Volume is not checked — partial bars often lack volume but their OHLC
    is still good enough to compute indicators.
    """
    if bars is None or len(bars) == 0:
        return False, "no bars available"

    for col in _REQUIRED_OHLC:
        if col not in bars.columns:
            return False, f"required column missing: {col}"

    n = len(bars)
    threshold = max_missing_pct / 100.0
    for col in _REQUIRED_OHLC:
        nan_count = int(bars[col].isna().sum())
        if nan_count / n > threshold:
            pct = 100.0 * nan_count / n
            return False, (
                f"too many missing values in {col}: {pct:.1f}% > {max_missing_pct:.1f}%"
            )
    return True, ""


def snapshot_id_for_bars(symbol: str, bars: pd.DataFrame) -> str:
    """Deterministic identifier for a (symbol, bars) pair. Used in the
    AuditObject's ``data_snapshot_ids`` so a future replay can reconstruct
    exactly what data the decision saw.

    Format: ``<symbol>:<n_bars>:<last_ts>:<short_hash>``.
    """
    if bars is None or len(bars) == 0:
        return f"{symbol}:0:none:00000000"
    last_ts = bars.index.max()
    last_str = last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts)
    fingerprint = (
        f"{symbol}|{len(bars)}|{last_str}|"
        f"{float(bars['close'].iloc[-1]):.4f}"
    )
    h = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:8]
    return f"{symbol}:{len(bars)}:{last_str}:{h}"
