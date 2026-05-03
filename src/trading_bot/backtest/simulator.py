"""Day-by-day backtest replay.

Reuses live code paths verbatim:
- `MomentumStrategy.evaluate` / `MeanReversionStrategy.evaluate` for entry signals
- `RiskManager.check` for pre-trade gating
- `regime.detect_regime_from_bars` for daily regime
- `market_data.compute_indicators` for indicator math

The new logic is only:
1. The day-by-day clock loop
2. The simulated portfolio
3. The bracket-leg exit simulator (stop wins on conflict; time-based fallback)
4. The simulated RiskState (daily/weekly P&L roll-up + halt flag)
"""
from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Session

from trading_bot.shared.alpaca_client import AccountSnapshot, AssetClass, OrderRequest, OrderSide, Position
from trading_bot.backtest.bar_store import BarStore
from trading_bot.shared.config import AppConfig
from trading_bot.exceptions import RiskRuleViolation
from trading_bot.market_data import MIN_BARS_FOR_INDICATORS, compute_indicators
from trading_bot.regime import detect_regime_from_bars
from trading_bot.shared.risk_manager import RiskManager, RiskState
from trading_bot.strategy import (
    MeanReversionStrategy,
    MomentumStrategy,
    SignalAction,
    strategy_for_regime,
)


# ---- public types ------------------------------------------------------


@dataclass(frozen=True)
class BacktestTrade:
    run_id: str
    symbol: str
    asset_class: str
    strategy: str
    regime_at_entry: str
    entry_date: date
    exit_date: date
    hold_days: int
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    exit_reason: str  # "stop" | "tp" | "time"
    realized_pnl: Decimal
    pnl_pct: float
    equity_at_entry: Decimal
    daily_pnl_pct_at_entry: float
    reason: str


@dataclass
class BacktestRunResult:
    run_id: str
    generated_at: datetime
    from_date: date
    to_date: date
    symbols: list[str]
    strategies_used: list[str]
    equity_curve: list[tuple[date, Decimal]]
    trades: list[BacktestTrade] = field(default_factory=list)
    skipped_by_risk: int = 0
    skipped_no_bars: int = 0
    halted_days: int = 0
    starting_equity: Decimal = Decimal("15000")
    ending_equity: Decimal = Decimal("15000")


# ---- persistence (mirror ClosedTradeStore shape) -----------------------


class _Base(DeclarativeBase):
    pass


class _BTRow(_Base):
    __tablename__ = "backtest_trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, index=True, nullable=False)
    symbol = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    strategy = Column(String, nullable=False)
    regime_at_entry = Column(String, nullable=False)
    entry_date = Column(Date, nullable=False)
    exit_date = Column(Date, nullable=False)
    hold_days = Column(Integer, nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=False)
    take_profit_price = Column(Float, nullable=False)
    exit_reason = Column(String, nullable=False)
    realized_pnl = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    equity_at_entry = Column(Float, nullable=False)
    daily_pnl_pct_at_entry = Column(Float, nullable=False)
    reason = Column(String, nullable=False)
    written_at = Column(DateTime, nullable=False)


class BacktestStore:
    """Append-only SQLite store of synthetic trades. Idempotent by
    (run_id, entry_order_id) — but we don't track order ids in the
    backtest, so dedup is by (run_id, symbol, entry_date, strategy)."""

    def __init__(self, db_path: Path | str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{path}", future=True)
        _Base.metadata.create_all(self._engine)

    def append(self, trade: BacktestTrade) -> None:
        with Session(self._engine) as s:
            existing = s.execute(
                select(_BTRow)
                .where(_BTRow.run_id == trade.run_id)
                .where(_BTRow.symbol == trade.symbol)
                .where(_BTRow.entry_date == trade.entry_date)
                .where(_BTRow.strategy == trade.strategy)
            ).scalar_one_or_none()
            if existing:
                return
            s.add(_BTRow(
                run_id=trade.run_id, symbol=trade.symbol,
                asset_class=trade.asset_class, strategy=trade.strategy,
                regime_at_entry=trade.regime_at_entry,
                entry_date=trade.entry_date, exit_date=trade.exit_date,
                hold_days=trade.hold_days, qty=float(trade.qty),
                entry_price=float(trade.entry_price),
                exit_price=float(trade.exit_price),
                stop_price=float(trade.stop_price),
                take_profit_price=float(trade.take_profit_price),
                exit_reason=trade.exit_reason,
                realized_pnl=float(trade.realized_pnl),
                pnl_pct=trade.pnl_pct,
                equity_at_entry=float(trade.equity_at_entry),
                daily_pnl_pct_at_entry=trade.daily_pnl_pct_at_entry,
                reason=trade.reason,
                written_at=datetime.utcnow(),
            ))
            s.commit()

    def by_run(self, run_id: str) -> list[BacktestTrade]:
        with Session(self._engine) as s:
            rows = s.execute(
                select(_BTRow).where(_BTRow.run_id == run_id).order_by(_BTRow.entry_date)
            ).scalars().all()
        return [
            BacktestTrade(
                run_id=r.run_id, symbol=r.symbol, asset_class=r.asset_class,
                strategy=r.strategy, regime_at_entry=r.regime_at_entry,
                entry_date=r.entry_date, exit_date=r.exit_date,
                hold_days=r.hold_days, qty=Decimal(str(r.qty)),
                entry_price=Decimal(str(r.entry_price)),
                exit_price=Decimal(str(r.exit_price)),
                stop_price=Decimal(str(r.stop_price)),
                take_profit_price=Decimal(str(r.take_profit_price)),
                exit_reason=r.exit_reason,
                realized_pnl=Decimal(str(r.realized_pnl)),
                pnl_pct=r.pnl_pct,
                equity_at_entry=Decimal(str(r.equity_at_entry)),
                daily_pnl_pct_at_entry=r.daily_pnl_pct_at_entry,
                reason=r.reason,
            )
            for r in rows
        ]


# ---- VIX history fetch -------------------------------------------------


_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch_vix_history(from_date: date, to_date: date, *, timeout: int = 15) -> dict[date, float]:
    """Pull FRED VIXCLS into a {date -> float} map. Returns empty on failure
    (the simulator falls back to bars-only regime detection)."""
    try:
        r = requests.get(
            _FRED_CSV,
            params={"id": "VIXCLS"},
            timeout=timeout,
            headers={"User-Agent": "trading-bot-backtest/0.1"},
        )
        r.raise_for_status()
    except Exception:
        return {}
    out: dict[date, float] = {}
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    for row in rows[1:]:  # skip header
        if len(row) < 2:
            continue
        try:
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < from_date or d > to_date:
            continue
        try:
            out[d] = float(row[1])
        except ValueError:
            continue
    return out


def _vix_at(vix_series: dict[date, float], on: date) -> float | None:
    """Lookup with up-to-7-day backfill — VIX is sometimes stale on holidays."""
    for delta in range(0, 7):
        d = on - timedelta(days=delta)
        if d in vix_series:
            return vix_series[d]
    return None


# ---- internal portfolio model ------------------------------------------


@dataclass
class _Position:
    symbol: str
    asset_class: str
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    entry_date: date
    regime_at_entry: str
    strategy_name: str
    reason: str
    equity_at_entry: Decimal
    daily_pnl_pct_at_entry: float
    peak_unrealized_pct: float = 0.0  # ratcheting watermark for trailing-stop logic


@dataclass
class _Portfolio:
    equity: Decimal
    cash: Decimal
    positions: list[_Position] = field(default_factory=list)


def _to_account_snapshot(p: _Portfolio) -> AccountSnapshot:
    return AccountSnapshot(
        equity=p.equity, cash=p.cash,
        buying_power=p.cash, portfolio_value=p.equity,
    )


def _to_alpaca_positions(p: _Portfolio) -> list[Position]:
    """Convert internal _Position objects into the alpaca-client Position
    shape that RiskManager.check expects. ``current_price`` is required;
    the simulator doesn't track an intraday last-trade price so we use
    ``entry_price`` (which makes ``unrealized_pl=0`` consistent — both are
    valued at entry). Production paper trading uses Alpaca's live mark."""
    return [
        Position(
            symbol=pos.symbol, qty=pos.qty,
            asset_class=pos.asset_class,
            avg_entry_price=pos.entry_price,
            current_price=pos.entry_price,
            market_value=pos.entry_price * pos.qty,
            unrealized_pl=Decimal("0"),
        )
        for pos in p.positions
    ]


# ---- the simulator -----------------------------------------------------


class Backtester:
    """Day-by-day replay. One instance per `run()` call.

    The contract is tight: only `run()` mutates state; everything else is a
    pure helper. That makes it easy to test exit-leg logic in isolation.
    """

    def __init__(
        self,
        config: AppConfig,
        bar_store: BarStore,
        *,
        starting_equity: Decimal = Decimal("15000"),
        max_hold_days: int = 60,
        slippage_bps: float = 0.0,
        vix_series: dict[date, float] | None = None,
        enable_trailing_stop: bool = False,  # off by default — empirical sweep
        trail_breakeven_pct: float = 3.0,    # showed every variant under-
        trail_lock_pct: float = 5.0,         # performs the no-trailing
        trail_giveback_fraction: float = 0.5,  # baseline on momentum/large-caps
        strategy_overrides: dict[str, dict] | None = None,
    ) -> None:
        self._cfg = config
        self._bars = bar_store
        self._starting_equity = starting_equity
        self._max_hold_days = max_hold_days
        self._slippage_bps = slippage_bps
        self._vix = vix_series or {}
        self._risk = RiskManager(config)
        self._trailing = enable_trailing_stop
        self._trail_be = trail_breakeven_pct
        self._trail_lock = trail_lock_pct
        self._trail_give = trail_giveback_fraction
        # Lab hook: param dict per template name. _strategy_for applies these.
        self._strategy_overrides = strategy_overrides or {}

    # -- public ----------------------------------------------------------

    def run(
        self,
        *,
        from_date: date,
        to_date: date,
        symbols: list[str],
        strategy_names: tuple[str, ...] = ("momentum", "mean_reversion"),
        benchmark: str = "SPY",
    ) -> BacktestRunResult:
        run_id = uuid.uuid4().hex[:12]
        portfolio = _Portfolio(equity=self._starting_equity, cash=self._starting_equity)
        result = BacktestRunResult(
            run_id=run_id,
            generated_at=datetime.now(timezone.utc),
            from_date=from_date, to_date=to_date,
            symbols=list(symbols),
            strategies_used=list(strategy_names),
            equity_curve=[],
            starting_equity=self._starting_equity,
        )

        # Use the benchmark's cached calendar as the trading-day clock.
        trading_dates = self._bars.trading_dates(
            benchmark, from_date=from_date, to_date=to_date
        )
        if not trading_dates:
            # No benchmark history — try the first symbol that has bars.
            for s in symbols:
                trading_dates = self._bars.trading_dates(s, from_date=from_date, to_date=to_date)
                if trading_dates:
                    break

        # Daily P&L history for weekly roll-up.
        daily_pnl_history: list[tuple[date, float]] = []
        consecutive_losing_days = 0
        prev_equity = self._starting_equity

        for d in trading_dates:
            day_start_equity = portfolio.equity
            realized_today = Decimal("0")

            # 1. Resolve any open positions against today's bar.
            survivors: list[_Position] = []
            for pos in portfolio.positions:
                outcome = self._resolve_exit(pos, on=d)
                if outcome is None:
                    survivors.append(pos)
                    continue
                exit_price, exit_reason = outcome
                exit_value = exit_price * pos.qty
                realized = (exit_price - pos.entry_price) * pos.qty
                realized_today += realized
                portfolio.cash += exit_value
                hold = (d - pos.entry_date).days
                pnl_pct = float((exit_price / pos.entry_price - 1) * 100) if pos.entry_price else 0.0
                result.trades.append(BacktestTrade(
                    run_id=run_id, symbol=pos.symbol, asset_class=pos.asset_class,
                    strategy=pos.strategy_name,
                    regime_at_entry=pos.regime_at_entry,
                    entry_date=pos.entry_date, exit_date=d,
                    hold_days=hold, qty=pos.qty,
                    entry_price=pos.entry_price, exit_price=exit_price,
                    stop_price=pos.stop_price, take_profit_price=pos.take_profit_price,
                    exit_reason=exit_reason,
                    realized_pnl=realized, pnl_pct=pnl_pct,
                    equity_at_entry=pos.equity_at_entry,
                    daily_pnl_pct_at_entry=pos.daily_pnl_pct_at_entry,
                    reason=pos.reason,
                ))
            portfolio.positions = survivors

            # 2. Mark-to-market remaining positions at today's close.
            mtm = Decimal("0")
            for pos in portfolio.positions:
                bar = self._bars.get_bar(pos.symbol, d)
                if bar is not None:
                    mtm += Decimal(str(bar.close)) * pos.qty
                else:
                    mtm += pos.entry_price * pos.qty  # stale fallback
            portfolio.equity = portfolio.cash + mtm

            daily_pct = (
                float(realized_today / day_start_equity * 100)
                if day_start_equity > 0 else 0.0
            )
            daily_pnl_history.append((d, daily_pct))
            weekly_pct = sum(p for _, p in daily_pnl_history[-5:])

            # 3. Update streaks.
            if daily_pct < 0:
                consecutive_losing_days += 1
            elif daily_pct > 0:
                consecutive_losing_days = 0

            # 4. Compute regime + halt for entries.
            spy_bars = self._bars.get(benchmark, end_date=d, lookback_days=250)
            if len(spy_bars) >= 60:
                reading = detect_regime_from_bars(
                    spy_bars,
                    vix=_vix_at(self._vix, d),
                    vol_threshold_pct=self._cfg.regime.vol_threshold_pct,
                )
                regime = reading.regime.value
            else:
                regime = "sideways"

            halted = (
                daily_pct <= -self._cfg.risk.daily_loss_limit_pct
                or weekly_pct <= -self._cfg.risk.weekly_loss_limit_pct
            )
            if halted:
                result.halted_days += 1

            # 5. Pick strategy and try entries (skipped if halted).
            if not halted and regime in self._cfg.regime_allocations:
                strategy = self._strategy_for(regime, strategy_names)
                if strategy is not None:
                    self._try_entries(
                        date_=d, symbols=symbols, strategy=strategy,
                        strategy_name=type(strategy).__name__.lower().replace("strategy", ""),
                        regime=regime, portfolio=portfolio,
                        daily_pct=daily_pct, weekly_pct=weekly_pct,
                        consecutive_losing_days=consecutive_losing_days,
                        result=result,
                    )

            result.equity_curve.append((d, portfolio.equity))
            prev_equity = portfolio.equity

        result.ending_equity = portfolio.equity
        return result

    # -- helpers ---------------------------------------------------------

    def _strategy_for(self, regime: str, allowed: tuple[str, ...]):
        chosen = strategy_for_regime(regime)
        if chosen is None:
            return None
        if isinstance(chosen, MomentumStrategy) and "momentum" in allowed:
            override = self._strategy_overrides.get("momentum")
            if override:
                return MomentumStrategy.from_params(override)
            return chosen
        if isinstance(chosen, MeanReversionStrategy) and "mean_reversion" in allowed:
            return chosen
        return None

    def _resolve_exit(
        self, pos: _Position, *, on: date
    ) -> tuple[Decimal, str] | None:
        """Return (exit_price, reason) if the position closes today, else None.

        Trailing-stop logic (when enabled): tracks peak unrealized P&L %
        across the position's life. At peak ≥ trail_breakeven_pct, the stop
        ratchets up to entry (breakeven). At peak ≥ trail_lock_pct, the stop
        trails at trail_giveback_fraction of the peak above entry. Stops
        never move down — once raised, they stay.
        """
        bar = self._bars.get_bar(pos.symbol, on)
        if bar is None:
            if (on - pos.entry_date).days >= self._max_hold_days:
                return pos.entry_price, "time"
            return None

        low = Decimal(str(bar.low))
        high = Decimal(str(bar.high))
        close = Decimal(str(bar.close))

        # Update peak unrealized P&L based on the bar's high (the most-favorable
        # intra-bar price), then ratchet the stop if applicable.
        if self._trailing and pos.entry_price > 0:
            high_pct = float((high / pos.entry_price - 1) * 100)
            if high_pct > pos.peak_unrealized_pct:
                pos.peak_unrealized_pct = high_pct
                # Lock-in tier: trail at giveback% of peak gain above entry
                if pos.peak_unrealized_pct >= self._trail_lock:
                    locked_pct = pos.peak_unrealized_pct * self._trail_give
                    new_stop = pos.entry_price * (Decimal("1") + Decimal(str(locked_pct / 100)))
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop
                # Breakeven tier
                elif pos.peak_unrealized_pct >= self._trail_be:
                    if pos.entry_price > pos.stop_price:
                        pos.stop_price = pos.entry_price

        # Stop wins on conflict — conservative.
        if low <= pos.stop_price:
            return pos.stop_price, "stop"
        if high >= pos.take_profit_price:
            return pos.take_profit_price, "tp"
        if (on - pos.entry_date).days >= self._max_hold_days:
            return close, "time"
        return None

    def _try_entries(
        self,
        *,
        date_: date,
        symbols: list[str],
        strategy,
        strategy_name: str,
        regime: str,
        portfolio: _Portfolio,
        daily_pct: float,
        weekly_pct: float,
        consecutive_losing_days: int,
        result: BacktestRunResult,
    ) -> None:
        # Already-held symbols are skipped.
        held = {p.symbol for p in portfolio.positions}

        state = RiskState(
            daily_pnl_pct=Decimal(str(daily_pct)),
            weekly_pnl_pct=Decimal(str(weekly_pct)),
            consecutive_losing_days=consecutive_losing_days,
            halted=False,
        )

        for sym in symbols:
            if sym in held:
                continue
            bars = self._bars.get(sym, end_date=date_, lookback_days=60)
            if len(bars) < MIN_BARS_FOR_INDICATORS:
                result.skipped_no_bars += 1
                continue
            try:
                ind = compute_indicators(bars)
            except Exception:
                result.skipped_no_bars += 1
                continue

            sig = strategy.evaluate(sym, ind, equity=portfolio.equity)
            if sig.action != SignalAction.BUY:
                continue

            asset_class = AssetClass.CRYPTO if "/" in sym else AssetClass.STOCK
            order = OrderRequest(
                symbol=sym, qty=sig.qty, side=OrderSide.BUY,
                asset_class=asset_class,
                limit_price=sig.entry_price,
                stop_loss_price=sig.stop_loss_price,
            )
            try:
                self._risk.check(
                    order,
                    account=_to_account_snapshot(portfolio),
                    positions=_to_alpaca_positions(portfolio),
                    state=state,
                    regime=regime,
                )
            except RiskRuleViolation:
                result.skipped_by_risk += 1
                continue

            # Open at next-day open to avoid look-ahead bias.
            entry_bar = self._next_open_bar(sym, after=date_)
            if entry_bar is None:
                continue
            entry = Decimal(str(entry_bar.open)) * (Decimal("1") + Decimal(str(self._slippage_bps / 10000)))
            stop = sig.stop_loss_price  # set at signal time
            risk_per_share = entry - stop
            if risk_per_share <= 0:
                continue
            tp = entry + risk_per_share * Decimal("2")  # 2:1 R:R, mirrors live

            cost = entry * sig.qty
            if cost > portfolio.cash:
                # Can't afford — risk_manager already gated this on % terms,
                # but in a backtest the qty is computed off equity not cash.
                continue
            portfolio.cash -= cost
            portfolio.positions.append(_Position(
                symbol=sym, asset_class=asset_class.value,
                qty=sig.qty, entry_price=entry,
                stop_price=stop, take_profit_price=tp,
                entry_date=entry_bar.date,
                regime_at_entry=regime,
                strategy_name=strategy_name,
                reason=sig.reason,
                equity_at_entry=portfolio.equity,
                daily_pnl_pct_at_entry=daily_pct,
            ))

    def _next_open_bar(self, symbol: str, *, after: date):
        """First bar strictly after `after`. Walks up to 7 days for weekends."""
        for delta in range(1, 8):
            cand = after + timedelta(days=delta)
            bar = self._bars.get_bar(symbol, cand)
            if bar is not None:
                return bar
        return None
