"""Daily data-freshness audit.

Reports the age of every cache the three workflows depend on. Designed to
run as part of the existing 16:30 ET daily digest so the operator notices
silent staleness the same day instead of after a bad trade.

Returns a list of FreshnessFinding rows; the digest renders any with
``severity != 'ok'`` as a warning section.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


# (cache_name, db_path, query, max_age_hours, severity_note)
_CACHE_CHECKS: tuple[tuple[str, Path, str, float, str], ...] = (
    (
        "wheel_universe_cache",
        Path("data/state.db"),
        "SELECT MAX(cached_at) FROM wheel_universe_cache",
        24.0 * 14,  # 14d TTL
        "wheel build runs nightly @ 21:30 ET",
    ),
    (
        "option_iv_history",
        Path("data/state.db"),
        "SELECT MAX(recorded_at) FROM option_iv_history",
        24.0 + 2,  # iv_capture daily @ 09:45 ET; allow weekend gap
        "iv_capture runs @ 09:45 ET on weekdays",
    ),
    (
        "news_sentiment",
        Path("data/news_sentiment.db"),
        "SELECT MAX(cached_at) FROM news_sentiment",
        24.0,  # sentiment_warm runs twice daily on weekdays
        "news_warm runs @ 08:55 + 12:00 ET on weekdays",
    ),
    (
        "massive_grouped",
        Path("data/massive_grouped.db"),
        "SELECT MAX(trade_date) FROM grouped_bars",
        72.0,  # market closed weekends; allow Mon-after-Fri staleness
        "Polygon grouped refreshes @ 06:30 ET; data lags ~1 day",
    ),
)


@dataclass(frozen=True)
class FreshnessFinding:
    cache: str
    last_seen: str
    age_hours: float
    budget_hours: float
    severity: str  # "ok" | "stale" | "missing"
    note: str


def _age_hours(ts_str, now: dt.datetime) -> float | None:
    """Parse a SQLite timestamp/date string and return its age in hours."""
    if ts_str is None:
        return None
    if isinstance(ts_str, dt.datetime):
        ts = ts_str
    else:
        s = str(ts_str).replace("Z", "+00:00")
        try:
            ts = dt.datetime.fromisoformat(s)
        except ValueError:
            try:
                ts = dt.datetime.combine(
                    dt.date.fromisoformat(s), dt.time.min, dt.timezone.utc,
                )
            except ValueError:
                return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now - ts).total_seconds() / 3600.0


_OPPORTUNITIES_PATH = Path("strategy/opportunities.md")
_OPPORTUNITIES_BUDGET_HOURS = 12.0
_OPPORTUNITIES_NOTE = "premarket_rank @ 07:30 ET + midday_rerank @ 12:00 ET on weekdays"


def _audit_opportunities_md(now: dt.datetime) -> FreshnessFinding:
    """Bucket B: add opportunities.md to the freshness audit. The equity scan
    lane reads its top-N from this file; if it's stale, the bot acts on
    yesterday's signal. The intel_scan path falls back to CORE_LIQUID_TICKERS
    when this is missing/stale, so detection here lets the operator notice
    the underlying job (premarket_rank / midday_rerank) failed.
    """
    cache = "opportunities_md"
    if not _OPPORTUNITIES_PATH.exists():
        return FreshnessFinding(
            cache=cache, last_seen="-", age_hours=float("inf"),
            budget_hours=_OPPORTUNITIES_BUDGET_HOURS, severity="missing",
            note=f"file missing: {_OPPORTUNITIES_PATH}",
        )
    try:
        head = _OPPORTUNITIES_PATH.read_text()[:500]
    except OSError as e:
        return FreshnessFinding(
            cache=cache, last_seen="-", age_hours=float("inf"),
            budget_hours=_OPPORTUNITIES_BUDGET_HOURS, severity="missing",
            note=f"read failed: {e}",
        )
    m = re.search(r"^Generated:\s*(\S+)", head, re.MULTILINE)
    if not m:
        return FreshnessFinding(
            cache=cache, last_seen="-", age_hours=float("inf"),
            budget_hours=_OPPORTUNITIES_BUDGET_HOURS, severity="missing",
            note="no Generated: header line",
        )
    age = _age_hours(m.group(1), now)
    if age is None:
        return FreshnessFinding(
            cache=cache, last_seen="-", age_hours=float("inf"),
            budget_hours=_OPPORTUNITIES_BUDGET_HOURS, severity="missing",
            note=_OPPORTUNITIES_NOTE,
        )
    severity = "ok" if age <= _OPPORTUNITIES_BUDGET_HOURS else "stale"
    return FreshnessFinding(
        cache=cache, last_seen=m.group(1),
        age_hours=age, budget_hours=_OPPORTUNITIES_BUDGET_HOURS,
        severity=severity, note=_OPPORTUNITIES_NOTE,
    )


def audit_freshness(now: dt.datetime | None = None) -> list[FreshnessFinding]:
    """Walk every cache and return per-cache freshness verdicts."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[FreshnessFinding] = []
    out.append(_audit_opportunities_md(now))
    for cache, db_path, query, budget, note in _CACHE_CHECKS:
        if not db_path.exists():
            out.append(FreshnessFinding(
                cache=cache, last_seen="-", age_hours=float("inf"),
                budget_hours=budget, severity="missing",
                note=f"db file missing: {db_path}",
            ))
            continue
        try:
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(query).fetchone()
            ts = row[0] if row else None
        except Exception as e:
            out.append(FreshnessFinding(
                cache=cache, last_seen="-", age_hours=float("inf"),
                budget_hours=budget, severity="missing",
                note=f"query failed: {e}",
            ))
            continue
        age = _age_hours(ts, now)
        if age is None:
            out.append(FreshnessFinding(
                cache=cache, last_seen="-", age_hours=float("inf"),
                budget_hours=budget, severity="missing", note=note,
            ))
            continue
        severity = "ok" if age <= budget else "stale"
        out.append(FreshnessFinding(
            cache=cache, last_seen=str(ts),
            age_hours=age, budget_hours=budget,
            severity=severity, note=note,
        ))
    return out


def render_text_summary(findings: list[FreshnessFinding]) -> str:
    """One-line-per-cache human summary. Used by CLI + email digest."""
    lines = ["Freshness audit:"]
    worst = "ok"
    for f in findings:
        marker = {"ok": "✓", "stale": "✗", "missing": "?"}[f.severity]
        if f.severity != "ok":
            worst = f.severity
        if f.age_hours == float("inf"):
            lines.append(f"  {marker} {f.cache:24s} MISSING  ({f.note})")
        else:
            lines.append(
                f"  {marker} {f.cache:24s} age={f.age_hours:>6.1f}h  "
                f"budget={f.budget_hours:>5.1f}h  {f.note}"
            )
    lines.append(f"Worst: {worst}")
    return "\n".join(lines)
