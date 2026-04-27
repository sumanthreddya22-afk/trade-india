import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

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
from trading_bot.strategy import MomentumStrategy, SignalAction, strategy_for_regime
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


def load_ranked_watchlist(path: Path) -> list[WatchlistEntry]:
    """Parse strategy/opportunities.md and return WatchlistEntry list in rank order.

    Entries look like:
        ### 1. NVDA (us_equity)
        ### 2. BTC/USD (crypto)
    """
    if not path.exists():
        return []
    text = path.read_text()
    out: list[WatchlistEntry] = []
    pattern = re.compile(r"^###\s+\d+\.\s+(\S+)\s+\(([^)]+)\)\s*$", re.MULTILINE)
    for match in pattern.finditer(text):
        symbol = match.group(1)
        asset_class_raw = match.group(2)
        # Preserve the raw asset_class value; only normalise crypto variants.
        if "crypto" in asset_class_raw.lower():
            asset_class = "crypto"
        else:
            asset_class = asset_class_raw
        out.append(WatchlistEntry(symbol=symbol, asset_class=asset_class, notes=""))
    return out


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
        state_builder=None,  # callable returning RiskState; if None, uses safe defaults
    ) -> None:
        self._cfg = config
        self._market = market_data
        self._alpaca = alpaca
        self._journal = journal
        self._regime = regime
        # Strategy is chosen by regime if not explicitly provided
        self._strategy = strategy or strategy_for_regime(regime)
        self._risk = risk_manager or RiskManager(config)
        self._state_builder = state_builder

    def _build_state(self) -> RiskState:
        if self._state_builder is not None:
            return self._state_builder()
        return RiskState(
            daily_pnl_pct=Decimal("0"),
            weekly_pnl_pct=Decimal("0"),
            consecutive_losing_days=0,
            halted=False,
        )

    def scan(self, *, watchlist: list[WatchlistEntry]) -> ScanResult:
        account = self._alpaca.get_account()
        positions = self._alpaca.get_positions()
        try:
            open_order_symbols = self._alpaca.get_open_order_symbols()
        except AlpacaClientError:
            open_order_symbols = set()
        state = self._build_state()
        decisions: list[Decision] = []

        if self._strategy is None:
            # Regime has no enabled strategy (e.g. sideways/risk_off after
            # the Plan-5b backtest disabled mean_reversion). Skip entries;
            # existing positions still managed by their bracket orders.
            for entry in watchlist:
                decisions.append(Decision(
                    symbol=entry.symbol, action="hold",
                    reason=f"no strategy enabled for regime {self._regime}",
                ))
            return ScanResult(decisions=decisions, timestamp=datetime.now(timezone.utc))

        for entry in watchlist:
            symbol = entry.symbol
            if has_open_position(symbol, positions):
                decisions.append(Decision(symbol=symbol, action="skipped_existing_position"))
                continue
            # Also skip if there's already a pending open order for this symbol
            if symbol in open_order_symbols or symbol.replace("/", "") in open_order_symbols:
                decisions.append(Decision(symbol=symbol, action="skipped_pending_order"))
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

            # News-sentiment gate (Plan 6c). When sentiment_floor is set,
            # skip entries on names with recent-news score below floor.
            # Crypto bypasses (Massive's news endpoint is equity-focused).
            sf = getattr(self._cfg.strategy, "sentiment_floor", None)
            if sf is not None and entry.asset_class != "crypto":
                from trading_bot.news_sentiment import score_for, passes_filter
                score = score_for(
                    symbol,
                    max_age_days=self._cfg.strategy.sentiment_max_age_days,
                )
                if not passes_filter(score, floor=sf):
                    decisions.append(Decision(
                        symbol=symbol, action="skipped_sentiment",
                        reason=f"news score {score:.2f} < floor {sf:.2f}",
                    ))
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
