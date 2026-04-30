"""W6 — `bot diagnose SYMBOL` operational CLI.

Reads the ``decisions`` table (W1.2) and produces a unified timeline so
the operator can answer "why didn't NVDA enter today?" from a single
shell command instead of grepping ``runs/`` + the news cache + the trade
journal separately.

Lives as a library function ``build_symbol_timeline`` plus a thin CLI
wrapper. Tests target the library; the CLI is a one-liner.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading_bot.decisions_store import DecisionRow, DecisionStore


@dataclass(frozen=True)
class TimelineEntry:
    timestamp_utc: datetime
    symbol: str
    action: str
    reason: str
    strategy: str
    audit_summary: str  # one-line audit fingerprint


@dataclass(frozen=True)
class SymbolTimeline:
    symbol: str
    entries: tuple[TimelineEntry, ...]
    summary: str

    def render(self) -> str:
        lines = [self.summary, ""]
        if not self.entries:
            lines.append(f"  (no decisions found for {self.symbol})")
            return "\n".join(lines)
        for e in self.entries:
            lines.append(
                f"  {e.timestamp_utc.isoformat()}  "
                f"[{e.action:24s}]  {e.symbol}  ({e.strategy})"
            )
            if e.reason:
                lines.append(f"      reason: {e.reason}")
            if e.audit_summary:
                lines.append(f"      audit:  {e.audit_summary}")
        return "\n".join(lines)


def _format_audit(row: DecisionRow) -> str:
    try:
        audit = json.loads(row.audit_json or "{}")
    except Exception:
        return ""
    parts: list[str] = []
    if audit.get("policy_version"):
        parts.append(f"policy_version={audit['policy_version']}")
    if audit.get("strategy_version"):
        parts.append(f"strategy_version={audit['strategy_version']}")
    if audit.get("regime"):
        parts.append(f"regime={audit['regime']}")
    return " ".join(parts)


def build_symbol_timeline(
    store: DecisionStore,
    symbol: str,
    *,
    limit: int = 200,
) -> SymbolTimeline:
    """Return the chronological decision history for ``symbol`` up to ``limit``
    entries. Decoys (other symbols) are filtered out."""
    rows = store.recent(limit=limit)
    matches = [r for r in rows if r.symbol.upper() == symbol.upper()]
    matches.sort(key=lambda r: r.timestamp_utc)
    entries = tuple(
        TimelineEntry(
            timestamp_utc=r.timestamp_utc,
            symbol=r.symbol,
            action=r.action,
            reason=r.reason,
            strategy=r.strategy,
            audit_summary=_format_audit(r),
        )
        for r in matches
    )
    summary = (
        f"{symbol} — {len(entries)} decision(s) in last {limit} bot decisions"
    )
    return SymbolTimeline(symbol=symbol, entries=entries, summary=summary)


def main(argv: list[str] | None = None) -> int:
    """Entry point used by ``bot diagnose <SYMBOL>`` (wired in cli.py).

    Standalone fallback path when invoked directly:
        python -m trading_bot.diagnose <SYMBOL>
    """
    import argparse

    parser = argparse.ArgumentParser(prog="diagnose")
    parser.add_argument("symbol", help="Symbol to diagnose (e.g. NVDA)")
    parser.add_argument("--db", default="data/state.db", help="state.db path")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    store = DecisionStore(Path(args.db))
    timeline = build_symbol_timeline(store, args.symbol, limit=args.limit)
    print(timeline.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
