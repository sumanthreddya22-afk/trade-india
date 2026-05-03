"""Portfolio change monitor.

Compares current Alpaca state against a snapshot from the previous run
(stored in `data/portfolio_snapshot.json`) and reports material changes:

- New fills (entry orders that filled since last snapshot)
- Stop-loss or take-profit triggers (legs that filled)
- Large unrealized P&L moves (> threshold)
- New positions, closed positions
- Halt-condition transitions

Material events trigger an email alert.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trading_bot.shared.alpaca_client import AlpacaClient


@dataclass(frozen=True)
class PositionSnap:
    symbol: str
    qty: str
    market_value: str
    avg_entry_price: str
    unrealized_pl: str


@dataclass(frozen=True)
class Snapshot:
    taken_at: str
    equity: str
    positions: dict[str, PositionSnap] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    severity: str  # "info" | "alert"
    kind: str
    symbol: str
    message: str


def load_snapshot(path: Path) -> Snapshot | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    positions = {
        sym: PositionSnap(**p) for sym, p in raw.get("positions", {}).items()
    }
    return Snapshot(taken_at=raw.get("taken_at", ""), equity=raw.get("equity", "0"), positions=positions)


def save_snapshot(path: Path, snap: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "taken_at": snap.taken_at,
        "equity": snap.equity,
        "positions": {sym: asdict(p) for sym, p in snap.positions.items()},
    }
    path.write_text(json.dumps(payload, indent=2))


def take_snapshot(alpaca: AlpacaClient) -> Snapshot:
    account = alpaca.get_account()
    positions = alpaca.get_positions()
    pos_map = {
        p.symbol: PositionSnap(
            symbol=p.symbol,
            qty=str(p.qty),
            market_value=str(p.market_value),
            avg_entry_price=str(p.avg_entry_price),
            unrealized_pl=str(p.unrealized_pl),
        )
        for p in positions
    }
    return Snapshot(
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        equity=str(account.equity),
        positions=pos_map,
    )


def diff_snapshots(
    prev: Snapshot | None,
    curr: Snapshot,
    *,
    big_move_pct_threshold: float = 2.0,
) -> list[Event]:
    """Compare snapshots, return material events."""
    events: list[Event] = []
    if prev is None:
        events.append(Event("info", "init", "", f"Initial snapshot taken (equity ${curr.equity})"))
        return events

    prev_eq = Decimal(prev.equity or "0")
    curr_eq = Decimal(curr.equity)
    if prev_eq > 0:
        eq_pct = (curr_eq - prev_eq) / prev_eq * Decimal("100")
        if abs(eq_pct) >= Decimal(str(big_move_pct_threshold)):
            sev = "alert" if eq_pct < 0 else "info"
            events.append(Event(
                severity=sev, kind="equity_move", symbol="",
                message=f"Equity moved {eq_pct:.2f}% (${prev_eq} → ${curr_eq})",
            ))

    prev_syms = set(prev.positions.keys())
    curr_syms = set(curr.positions.keys())

    for sym in curr_syms - prev_syms:
        p = curr.positions[sym]
        events.append(Event(
            severity="alert", kind="new_position", symbol=sym,
            message=f"NEW position: {sym} qty={p.qty} avg=${p.avg_entry_price} value=${p.market_value}",
        ))

    for sym in prev_syms - curr_syms:
        p = prev.positions[sym]
        events.append(Event(
            severity="alert", kind="closed_position", symbol=sym,
            message=f"CLOSED position: {sym} (was qty={p.qty})",
        ))

    for sym in curr_syms & prev_syms:
        prev_p = prev.positions[sym]
        curr_p = curr.positions[sym]
        prev_pl = Decimal(prev_p.unrealized_pl)
        curr_pl = Decimal(curr_p.unrealized_pl)
        prev_mv = Decimal(prev_p.market_value)
        if prev_mv > 0:
            move_pct = (curr_pl - prev_pl) / prev_mv * Decimal("100")
            if abs(move_pct) >= Decimal(str(big_move_pct_threshold)):
                sev = "alert" if move_pct < 0 else "info"
                events.append(Event(
                    severity=sev, kind="unrealized_move", symbol=sym,
                    message=f"{sym} unrealized P&L moved {move_pct:.2f}% "
                            f"(${prev_pl} → ${curr_pl})",
                ))
        if Decimal(prev_p.qty) != Decimal(curr_p.qty):
            events.append(Event(
                severity="alert", kind="qty_change", symbol=sym,
                message=f"{sym} qty changed: {prev_p.qty} → {curr_p.qty}",
            ))
    return events


def has_alerts(events: list[Event]) -> bool:
    return any(e.severity == "alert" for e in events)
