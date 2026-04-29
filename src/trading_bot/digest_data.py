"""Gather live data for the daily digest + midday snapshot.

Single source of truth for all the numbers that go into operator emails.
Each gather_* function pulls from the real data layer (Alpaca portfolio
history, trade journal, state DB, news sentiment cache, FRED macro,
options state). All defensively wrapped — a missing source returns
neutral defaults (zeros / empty lists) rather than blowing up the email.

Used by:
  * cli.daily_digest         — full EOD report
  * cli.midday_snapshot_cli  — light intraday update
  * reports.run_eod          — daemon's daily_digest cron runner
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from trading_bot.alpaca_client import AlpacaClient
from trading_bot.config import AppConfig, Settings


log = logging.getLogger(__name__)


@dataclass
class DigestData:
    """All real-world numbers for the daily digest + midday snapshot.
    Defaults are zeros/empty so callers can use field-by-field."""
    # Equity / P&L
    starting_equity: Decimal = Decimal("0")     # yesterday's closing equity
    ending_equity: Decimal = Decimal("0")       # current equity
    realized_pnl: Decimal = Decimal("0")        # today's realized
    unrealized_pnl: Decimal = Decimal("0")      # sum of open-position unrealized
    daily_pnl_pct: float = 0.0                  # today's P&L as % (vs yesterday close)
    weekly_pnl_pct: float = 0.0                 # week P&L as %
    drawdown_pct: float = 0.0                   # peak-to-current as %
    consecutive_losing_days: int = 0
    equity_30d: list[Decimal] = field(default_factory=list)  # last 30 daily-close points

    # Macro
    vix: float | None = None
    yield_10y: float | None = None

    # Trades + positions
    trades_today: list = field(default_factory=list)         # list[TradeRow]
    positions: list[dict] = field(default_factory=list)
    closed_trades_7d: list[dict] = field(default_factory=list)

    # Errors / daemon health
    errors: list[str] = field(default_factory=list)
    daemon_blips: int = 0

    # Wheel
    wheel_open_cycles: list[dict] = field(default_factory=list)
    wheel_pnl_mtd: Decimal = Decimal("0")
    wheel_collateral_pct: float = 0.0
    wheel_win_rate: float = 0.0


def gather_all(*, settings: Settings, app_cfg: AppConfig,
               today: dt.date | None = None) -> DigestData:
    """One-call gather. Each section is wrapped so a single failing source
    doesn't kill the whole email. Returns DigestData with whatever was
    successfully gathered."""
    today = today or dt.date.today()
    out = DigestData()

    alpaca = AlpacaClient(settings)
    try:
        account = alpaca.get_account()
        out.ending_equity = account.equity
    except Exception as e:
        log.warning("digest_data: account fetch failed: %s", e)
        return out  # without equity nothing else makes sense

    _gather_portfolio_history(out, settings)
    _gather_pnl(out, settings, app_cfg)
    _gather_positions_and_unrealized(out, alpaca)
    _gather_macro(out)
    _gather_trades_today(out, app_cfg, today)
    _gather_closed_trades_7d(out)
    _gather_errors_and_blips(out, today)
    _gather_wheel(out, today)
    return out


# ── individual gatherers ─────────────────────────────────────────────

def _gather_portfolio_history(out: DigestData, settings: Settings) -> None:
    """Pull last 30 daily-close equity points from Alpaca portfolio history.
    Sets equity_30d AND starting_equity (yesterday's close) AND drawdown_pct."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        tc = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            paper=True,
        )
        hist = tc.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        )
        clean = [Decimal(str(e)) for e in (hist.equity or [])
                 if e is not None and e > 0]
        out.equity_30d = clean[-30:]
        if len(clean) >= 2:
            # Yesterday's closing equity = penultimate point
            out.starting_equity = clean[-2]
        elif clean:
            out.starting_equity = clean[-1]
        else:
            out.starting_equity = out.ending_equity

        # Drawdown: peak-to-current over the captured window
        if clean:
            peak = max(clean)
            current = clean[-1]
            if peak > 0:
                out.drawdown_pct = float((peak - current) / peak * 100)
    except Exception as e:
        log.warning("digest_data: portfolio history failed: %s", e)
        out.starting_equity = out.ending_equity


def _gather_pnl(out: DigestData, settings: Settings, app_cfg: AppConfig) -> None:
    """Daily/weekly P&L percentages + consecutive losing days."""
    try:
        from trading_bot.pnl_state import PnlStateBuilder
        r = PnlStateBuilder(settings, app_cfg).read()
        out.daily_pnl_pct = float(r.daily_pnl_pct)
        out.weekly_pnl_pct = float(r.weekly_pnl_pct)
        out.consecutive_losing_days = int(r.consecutive_losing_days)
    except Exception as e:
        log.warning("digest_data: pnl_state failed: %s", e)


def _gather_positions_and_unrealized(out: DigestData, alpaca: AlpacaClient) -> None:
    """Open positions + sum of unrealized P&L."""
    try:
        positions = alpaca.get_positions()
    except Exception as e:
        log.warning("digest_data: positions failed: %s", e)
        return

    pos_dicts: list[dict] = []
    total_unrealized = Decimal("0")
    for p in positions:
        try:
            total_unrealized += p.unrealized_pl
        except Exception:
            pass
        try:
            cls = str(p.asset_class).split(".")[-1].lower()
        except Exception:
            cls = "stock"
        # P&L percentages
        try:
            entry_px = float(p.avg_entry_price)
            curr_px = float(p.current_price)
            total_pct = ((curr_px - entry_px) / entry_px * 100) if entry_px else 0.0
        except Exception:
            total_pct = 0.0
        side = "long" if float(p.qty) >= 0 else "short"
        # Schema expected by email_digest.py Positions section.
        pos_dicts.append({
            "symbol": p.symbol,
            "qty": str(p.qty),
            "side": side,
            "entry": f"${float(p.avg_entry_price):,.2f}",
            "current": f"${float(p.current_price):,.2f}",
            "today_pct": "—",     # intra-day move requires another data source
            "total_pct": f"{total_pct:+.2f}%",
            "stop": "—",          # bot mandates stops; surfaced once journal join is wired
            "distance_pct": "—",
            "sentiment": "—",
            "sector": "—" if cls != "stock" else "—",
            # Auxiliary fields kept for other consumers (dashboard, etc.)
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "asset_class": cls,
        })
    out.positions = pos_dicts
    out.unrealized_pnl = total_unrealized


def _gather_macro(out: DigestData) -> None:
    """VIX + 10Y yield from FRED."""
    try:
        from trading_bot.intelligence import get_macro_snapshot
        m = get_macro_snapshot()
        out.vix = m.vix
        out.yield_10y = m.yield_10y_pct
    except Exception as e:
        log.warning("digest_data: macro failed: %s", e)


def _gather_trades_today(out: DigestData, app_cfg: AppConfig,
                         today: dt.date) -> None:
    """Today's entry trades from the journal, formatted as TradeRow."""
    try:
        from trading_bot.email_digest import TradeRow
        from trading_bot.trade_journal import TradeJournal
        journal = TradeJournal(Path(app_cfg.storage.trade_journal_path))
        all_trades = journal.all()
    except Exception as e:
        log.warning("digest_data: trade journal failed: %s", e)
        return

    rows = []
    for t in all_trades:
        try:
            ts = getattr(t, "timestamp", None)
            if ts is None or ts.date() != today:
                continue
            rows.append(TradeRow(
                side=str(getattr(t, "side", "BUY")),
                symbol=str(t.symbol),
                qty=Decimal(str(t.qty)),
                price=Decimal(str(getattr(t, "entry_price",
                                         getattr(t, "price", 0)))),
                strategy=str(getattr(t, "strategy",
                                     getattr(t, "reason", "momentum"))),
                time=ts.time().replace(microsecond=0),
                status="open",  # journal entries are entries; closed are separate
            ))
        except Exception as e:  # noqa: BLE001
            log.debug("digest_data: skipped trade row: %s", e)
    out.trades_today = rows


def _gather_closed_trades_7d(out: DigestData) -> None:
    """Closed trades from last 7 days for win-rate calc + 'closed trades'
    digest section."""
    try:
        from trading_bot.reconciliation import ClosedTradeStore
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
        store = ClosedTradeStore(Path("data/closed_trades.db"))
        all_closed = store.all()
    except Exception as e:
        log.warning("digest_data: closed_trades failed: %s", e)
        return

    rows: list[dict] = []
    for c in all_closed:
        try:
            exit_t = getattr(c, "exit_time", None)
            if exit_t is None or exit_t < cutoff:
                continue
            rows.append({
                "symbol": c.symbol,
                "side": c.side,
                "qty": float(c.qty),
                "entry_price": float(c.entry_price),
                "exit_price": float(c.exit_price),
                "pnl": float(c.realized_pnl),
                "pnl_pct": float(c.pnl_pct),
                "strategy": getattr(c, "strategy", ""),
                "exit_time": exit_t.isoformat() if exit_t else "",
                "hold_hours": float(getattr(c, "hold_hours", 0)),
            })
        except Exception:
            continue
    out.closed_trades_7d = rows


def _gather_errors_and_blips(out: DigestData, today: dt.date) -> None:
    """Today's daemon errors from state.db role_runs + on-disk error logs."""
    try:
        from sqlalchemy import text
        from trading_bot.state_db import get_engine
        db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
        engine = get_engine(db_path)
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT role_name, status, COUNT(*) as cnt, MAX(error_text) as last_err
                FROM role_runs
                WHERE started_at >= :since AND status != 'ok'
                GROUP BY role_name, status
                ORDER BY cnt DESC
            """), {"since": dt.datetime.combine(today, dt.time.min,
                                                tzinfo=dt.timezone.utc)
                   .isoformat()}).fetchall()
    except Exception as e:
        log.warning("digest_data: role_runs failed: %s", e)
        return

    blips = 0
    errors: list[str] = []
    for r in rows:
        role, status, cnt, last_err = r[0], r[1], int(r[2]), r[3]
        blips += cnt
        if status in ("halted", "error", "blocked") and last_err:
            errors.append(f"{role} ({status}, ×{cnt}): {(last_err or '')[:140]}")
    out.daemon_blips = blips
    out.errors = errors[:20]  # cap so the email isn't a wall of red


def _gather_wheel(out: DigestData, today: dt.date) -> None:
    """Wheel open cycles, MTD P&L, collateral %, win rate."""
    try:
        from sqlalchemy.orm import Session
        from trading_bot.options.wheel_state import WheelStateRepo
        from trading_bot.state_db import WheelCycle, get_engine
        db_path = os.environ.get("TRADING_BOT_STATE_DB", "data/state.db")
        engine = get_engine(db_path)
        repo = WheelStateRepo(engine)
        active = repo.list_active()
    except Exception as e:
        log.warning("digest_data: wheel state failed: %s", e)
        return

    cycles: list[dict] = []
    open_collateral = Decimal("0")
    for c in active:
        contract = c.cc_contract or c.csp_contract or "—"
        strike = c.cc_strike or c.csp_strike or Decimal("0")
        expiration = c.cc_expiration or c.csp_expiration
        credit = c.cc_credit or c.csp_credit or Decimal("0")
        cycles.append({
            "symbol": c.symbol, "phase": c.phase,
            "contract": contract,
            "strike": str(strike), "expiration": str(expiration) if expiration else "",
            "credit": str(credit),
            "delta": "—",  # would require live chain fetch; skip for digest
            "iv": "—",
            "dte": (expiration - today).days if expiration else 0,
            "mark": "",
            "pnl": "",
            "trigger_distance": "",
        })
        if strike:
            open_collateral += strike * Decimal(100)
    out.wheel_open_cycles = cycles

    if out.ending_equity > 0 and open_collateral > 0:
        out.wheel_collateral_pct = float(open_collateral / out.ending_equity * 100)

    # MTD wheel P&L + win rate from closed cycles this month
    try:
        month_start = dt.datetime(today.year, today.month, 1, tzinfo=dt.timezone.utc)
        with Session(engine) as s:
            closed = (s.query(WheelCycle)
                      .filter(WheelCycle.phase == "closed",
                              WheelCycle.closed_at >= month_start)
                      .all())
        if closed:
            out.wheel_pnl_mtd = sum(
                (c.realized_pnl or Decimal("0")) for c in closed
            )
            wins = sum(1 for c in closed if (c.realized_pnl or 0) > 0)
            out.wheel_win_rate = wins / len(closed)
    except Exception as e:
        log.debug("digest_data: wheel mtd/winrate failed: %s", e)
