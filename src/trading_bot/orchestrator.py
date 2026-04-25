from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading_bot.alpaca_client import (
    AlpacaClient,
    AssetClass,
    OrderRequest,
    OrderSide,
)
from trading_bot.config import AppConfig
from trading_bot.exceptions import AlpacaClientError, RiskRuleViolation
from trading_bot.market_data import (
    MIN_BARS_FOR_INDICATORS,
    MarketDataClient,
    compute_indicators,
)
from trading_bot.risk_manager import RiskManager, RiskState
from trading_bot.state import WatchlistEntry, has_open_position
from trading_bot.strategy import MomentumStrategy, SignalAction
from trading_bot.trade_journal import TradeJournal, TradeRecord


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: str
    reason: str = ""
    entry_order_id: str = ""
    stop_loss_order_id: str = ""


@dataclass(frozen=True)
class ScanResult:
    decisions: list[Decision]
    timestamp: datetime


class TradeOrchestrator:
    def __init__(
        self,
        *,
        config: AppConfig,
        market_data: MarketDataClient,
        alpaca: AlpacaClient,
        journal: TradeJournal,
        regime: str = "trending_up",
        strategy: MomentumStrategy | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self._cfg = config
        self._market = market_data
        self._alpaca = alpaca
        self._journal = journal
        self._regime = regime
        self._strategy = strategy or MomentumStrategy()
        self._risk = risk_manager or RiskManager(config)

    def _build_state(self) -> RiskState:
        return RiskState(
            daily_pnl_pct=Decimal("0"),
            weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0,
            halted=False,
        )

    def scan(self, *, watchlist: list[WatchlistEntry]) -> ScanResult:
        account = self._alpaca.get_account()
        positions = self._alpaca.get_positions()
        state = self._build_state()
        decisions: list[Decision] = []

        for entry in watchlist:
            symbol = entry.symbol
            if has_open_position(symbol, positions):
                decisions.append(Decision(symbol=symbol, action="skipped_existing_position"))
                continue

            try:
                bars = self._market.get_daily_bars(symbol, lookback_days=60)
            except AlpacaClientError as e:
                decisions.append(Decision(symbol=symbol, action="api_error", reason=str(e)))
                continue

            if len(bars) < MIN_BARS_FOR_INDICATORS:
                decisions.append(
                    Decision(symbol=symbol, action="skipped_insufficient_data",
                             reason=f"{len(bars)} bars < {MIN_BARS_FOR_INDICATORS}")
                )
                continue

            ind = compute_indicators(bars)
            sig = self._strategy.evaluate(symbol, ind, equity=account.equity)
            if sig.action != SignalAction.BUY:
                decisions.append(Decision(symbol=symbol, action="hold", reason=sig.reason))
                continue

            asset_class = AssetClass.CRYPTO if entry.asset_class == "crypto" else AssetClass.STOCK
            order = OrderRequest(
                symbol=symbol,
                qty=sig.qty,
                side=OrderSide.BUY,
                asset_class=asset_class,
                limit_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
            )

            try:
                self._risk.check(order, account=account, positions=positions,
                                 state=state, regime=self._regime)
            except RiskRuleViolation as e:
                decisions.append(Decision(symbol=symbol, action="rejected_by_risk",
                                          reason=f"{e.rule}: {e.detail}"))
                continue

            try:
                result = self._alpaca.place_order_with_stop_loss(order)
            except AlpacaClientError as e:
                decisions.append(Decision(symbol=symbol, action="api_error", reason=str(e)))
                continue

            self._journal.append(TradeRecord(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                side="buy",
                qty=sig.qty,
                price=sig.entry_price,
                asset_class=asset_class.value,
                strategy="momentum",
                regime=self._regime,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
                notes=sig.reason,
            ))
            decisions.append(Decision(
                symbol=symbol, action="placed_order", reason=sig.reason,
                entry_order_id=result.entry_order_id,
                stop_loss_order_id=result.stop_loss_order_id,
            ))

        return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))
