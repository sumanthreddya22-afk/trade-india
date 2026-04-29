"""ATM 30-day IV capture + IV-rank computation from local history."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import desc, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.state_db import OptionIvHistory


def capture_atm_iv_for_symbol(
    chain: list[ChainContract], *, spot: float, today: dt.date,
    target_dte: int = 30, dte_window: int = 7,
) -> float | None:
    """Pick ATM call+put pair closest to target_dte, return mean IV."""
    if not chain or spot <= 0:
        return None
    candidates = [c for c in chain
                  if abs((c.expiration - today).days - target_dte) <= dte_window]
    if not candidates:
        return None

    def by_strike_dte(c: ChainContract):
        return (abs(c.strike - spot), abs((c.expiration - today).days - target_dte))

    calls = sorted([c for c in candidates if c.kind == "C"], key=by_strike_dte)
    puts = sorted([c for c in candidates if c.kind == "P"], key=by_strike_dte)
    if not calls or not puts:
        return None
    iv_call = calls[0].implied_volatility
    iv_put = puts[0].implied_volatility
    return (iv_call + iv_put) / 2.0


def compute_iv_rank(
    engine: Engine, symbol: str, *, current_iv: float, min_history: int = 5,
    lookback_days: int = 252,
) -> float | None:
    """Return IV rank in [0, 100] vs trailing `lookback_days` of stored ATM IV.
    Returns None if local history < `min_history` rows."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    with Session(engine) as s:
        rows = s.execute(
            select(OptionIvHistory.atm_iv_30d)
            .where(OptionIvHistory.symbol == symbol,
                   OptionIvHistory.recorded_at >= cutoff)
            .order_by(desc(OptionIvHistory.recorded_at))
        ).scalars().all()
    if len(rows) < min_history:
        return None
    lo, hi = min(rows), max(rows)
    if hi <= lo:
        return 0.0 if current_iv <= lo else 100.0
    rank = (current_iv - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, rank))


def record_iv(engine: Engine, *, symbol: str, atm_iv: float, ts: dt.datetime | None = None) -> None:
    when = ts or dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        s.add(OptionIvHistory(symbol=symbol, recorded_at=when, atm_iv_30d=atm_iv))
        s.commit()
