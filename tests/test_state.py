from decimal import Decimal
from pathlib import Path

from trading_bot.alpaca_client import Position
from trading_bot.state import (
    WatchlistEntry,
    has_open_position,
    load_watchlist,
)


def test_load_watchlist_parses_yaml(tmp_path: Path):
    p = tmp_path / "watchlist.yaml"
    p.write_text(
        """
symbols:
  - symbol: SPY
    asset_class: stock
    notes: ETF
  - symbol: BTC/USD
    asset_class: crypto
    notes: BTC
"""
    )
    wl = load_watchlist(p)
    assert len(wl) == 2
    assert isinstance(wl[0], WatchlistEntry)
    assert wl[0].symbol == "SPY"
    assert wl[0].asset_class == "stock"
    assert wl[1].symbol == "BTC/USD"


def test_has_open_position_true():
    pos = Position(
        symbol="AAPL",
        qty=Decimal("3"),
        market_value=Decimal("585"),
        avg_entry_price=Decimal("195"),
        unrealized_pl=Decimal("0"),
        asset_class="us_equity",
    )
    assert has_open_position("AAPL", [pos]) is True


def test_has_open_position_false():
    assert has_open_position("AAPL", []) is False


def test_has_open_position_normalizes_crypto_symbol():
    pos = Position(
        symbol="BTCUSD",
        qty=Decimal("0.001"),
        market_value=Decimal("70"),
        avg_entry_price=Decimal("70000"),
        unrealized_pl=Decimal("0"),
        asset_class="crypto",
    )
    assert has_open_position("BTC/USD", [pos]) is True
