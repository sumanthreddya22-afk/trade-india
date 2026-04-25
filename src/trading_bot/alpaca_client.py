# src/trading_bot/alpaca_client.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from alpaca.trading.client import TradingClient

from trading_bot.config import Settings
from trading_bot.exceptions import AlpacaClientError, LiveModeDisabled

PAPER_URL_PREFIX = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class AccountSnapshot:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    market_value: Decimal
    avg_entry_price: Decimal
    unrealized_pl: Decimal
    asset_class: str


class AlpacaClient:
    """Wrapper around alpaca-py TradingClient. Paper-only by construction."""

    def __init__(self, settings: Settings) -> None:
        if not settings.alpaca_base_url.startswith(PAPER_URL_PREFIX):
            raise LiveModeDisabled()
        self._client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )

    def get_account(self) -> AccountSnapshot:
        try:
            a = self._client.get_account()
        except Exception as e:
            raise AlpacaClientError(f"get_account failed: {e}") from e
        return AccountSnapshot(
            equity=Decimal(str(a.equity)),
            cash=Decimal(str(a.cash)),
            buying_power=Decimal(str(a.buying_power)),
            portfolio_value=Decimal(str(a.portfolio_value)),
        )

    def get_positions(self) -> list[Position]:
        try:
            raw = self._client.get_all_positions()
        except Exception as e:
            raise AlpacaClientError(f"get_all_positions failed: {e}") from e
        return [
            Position(
                symbol=p.symbol,
                qty=Decimal(str(p.qty)),
                market_value=Decimal(str(p.market_value)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                unrealized_pl=Decimal(str(p.unrealized_pl)),
                asset_class=str(p.asset_class),
            )
            for p in raw
        ]
