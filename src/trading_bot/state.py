from dataclasses import dataclass
from pathlib import Path

import yaml

from trading_bot.shared.alpaca_client import Position
from trading_bot.exceptions import ConfigError


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    notes: str


def load_watchlist(path: Path) -> list[WatchlistEntry]:
    if not path.exists():
        raise ConfigError(f"watchlist not found: {path}")
    raw = yaml.safe_load(path.read_text())
    out: list[WatchlistEntry] = []
    for entry in raw.get("symbols", []):
        out.append(
            WatchlistEntry(
                symbol=entry["symbol"],
                asset_class=entry["asset_class"],
                notes=entry.get("notes", ""),
            )
        )
    return out


def _normalize(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def has_open_position(symbol: str, positions: list[Position]) -> bool:
    target = _normalize(symbol)
    return any(_normalize(p.symbol) == target for p in positions)
