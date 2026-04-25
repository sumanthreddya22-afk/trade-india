# src/trading_bot/alpaca_client.py
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide as AlpacaSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from pydantic import BaseModel, model_validator

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


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
    OPTION = "option"


class OrderRequest(BaseModel):
    symbol: str
    qty: Decimal
    side: OrderSide
    asset_class: AssetClass
    limit_price: Decimal
    stop_loss_price: Decimal | None

    @model_validator(mode="after")
    def require_stop_loss(self) -> "OrderRequest":
        if self.stop_loss_price is None:
            raise ValueError("stop_loss_price is required — every position must have a stop")
        return self


@dataclass(frozen=True)
class OrderResult:
    entry_order_id: str
    stop_loss_order_id: str


def _to_alpaca_side(s: OrderSide) -> AlpacaSide:
    return AlpacaSide.BUY if s == OrderSide.BUY else AlpacaSide.SELL


def _opposite(s: OrderSide) -> OrderSide:
    return OrderSide.SELL if s == OrderSide.BUY else OrderSide.BUY


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

    def get_open_order_symbols(self) -> set[str]:
        """Symbols with non-terminal open orders (used to prevent duplicate entries)."""
        try:
            orders = self._client.get_orders()
        except Exception as e:
            raise AlpacaClientError(f"get_orders failed: {e}") from e
        return {str(o.symbol) for o in orders}

    def place_order_with_stop_loss(self, req: OrderRequest) -> OrderResult:
        """Place atomic bracket order: entry + stop-loss together.

        Crypto doesn't support bracket orders on Alpaca, so for crypto we
        fall back to a market entry then a stop-loss as a separate order.
        """
        if req.asset_class == AssetClass.CRYPTO:
            return self._place_crypto_with_stop(req)
        return self._place_stock_bracket(req)

    def _place_stock_bracket(self, req: OrderRequest) -> OrderResult:
        # Alpaca bracket orders require a take-profit leg. Use 2:1 reward:risk.
        risk = abs(float(req.limit_price) - float(req.stop_loss_price))
        if req.side == OrderSide.BUY:
            take_profit_price = float(req.limit_price) + 2 * risk
        else:
            take_profit_price = float(req.limit_price) - 2 * risk
        try:
            entry_req = LimitOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.DAY,
                limit_price=float(req.limit_price),
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=float(req.stop_loss_price)),
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            )
            entry = self._client.submit_order(entry_req)
        except Exception as e:
            raise AlpacaClientError(f"bracket order failed: {e}") from e

        # Bracket orders return the parent (entry) order with `legs` containing
        # the stop-loss + take-profit legs. Find the stop leg.
        stop_id = ""
        legs = getattr(entry, "legs", None) or []
        for leg in legs:
            if str(getattr(leg, "type", "")).lower().endswith("stop"):
                stop_id = str(leg.id)
                break
        if not stop_id and legs:
            stop_id = str(legs[0].id)
        return OrderResult(entry_order_id=str(entry.id), stop_loss_order_id=stop_id)

    def _place_crypto_with_stop(self, req: OrderRequest) -> OrderResult:
        try:
            entry_req = MarketOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(req.side),
                time_in_force=TimeInForce.GTC,
            )
            entry = self._client.submit_order(entry_req)
        except Exception as e:
            raise AlpacaClientError(f"crypto entry order failed: {e}") from e

        try:
            stop_req = StopOrderRequest(
                symbol=req.symbol,
                qty=float(req.qty),
                side=_to_alpaca_side(_opposite(req.side)),
                time_in_force=TimeInForce.GTC,
                stop_price=float(req.stop_loss_price),
            )
            stop = self._client.submit_order(stop_req)
        except Exception as e:
            # Crypto market orders fill near-instantly; cancellation likely won't help.
            # Surface the error so the operator can manually flatten.
            raise AlpacaClientError(
                f"crypto stop-loss order failed (entry may already be filled — manually flatten): {e}"
            ) from e

        return OrderResult(entry_order_id=str(entry.id), stop_loss_order_id=str(stop.id))
