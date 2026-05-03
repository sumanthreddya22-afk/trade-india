"""Daily IV capture job.

Once per trading day (cron @ 9:45 ET, 15 min after the open settles), iterate
the eligible set (allowlist - blocklist) and fetch one option chain per symbol
to capture the ATM 30-day implied volatility. Append to `option_iv_history`.

This is the ONLY place in the wheel pipeline that mass-fetches chains. The
wheel-scan job that runs at 10:15 reads from `option_iv_history` to compute
IV-rank and surface candidates — it never iterates the broad universe.

Idempotent within a calendar day: at most one row per (symbol, date). Re-runs
safely overwrite same-day rows."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import and_
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from trading_bot.options.alpaca_options import OptionAlpacaClient
from trading_bot.options.chain import ChainContract
from trading_bot.options.iv_rank import capture_atm_iv_for_symbol
from trading_bot.state_db import OptionIvHistory


log = logging.getLogger(__name__)

# Capture chain for ~30 DTE only — that's the wheel's natural target window
# and gives stable IV reads that aren't whipped around by 0DTE noise.
_CAPTURE_DTE_LOOKAHEAD_LO = 25
_CAPTURE_DTE_LOOKAHEAD_HI = 35


@dataclass(frozen=True)
class IvCaptureDeps:
    option_alpaca: OptionAlpacaClient
    engine: Engine
    spot_for: Callable[[str], float | None]
    eligible: set[str]
    today: dt.date


def run_iv_capture(deps: IvCaptureDeps) -> int:
    """Capture today's ATM 30-day IV for each eligible symbol. Returns the
    number of (symbol, IV) rows actually written."""
    written = 0
    expiration_gte = deps.today + dt.timedelta(days=_CAPTURE_DTE_LOOKAHEAD_LO)
    expiration_lte = deps.today + dt.timedelta(days=_CAPTURE_DTE_LOOKAHEAD_HI)

    for symbol in sorted(deps.eligible):
        spot = deps.spot_for(symbol)
        if spot is None or spot <= 0:
            log.info("iv_capture skip %s: no spot", symbol)
            continue
        try:
            chain: list[ChainContract] = deps.option_alpaca.get_chain(
                symbol,
                expiration_gte=expiration_gte,
                expiration_lte=expiration_lte,
            )
        except Exception as e:  # noqa: BLE001 — best-effort capture
            log.warning("iv_capture chain fetch failed for %s: %s", symbol, e)
            continue
        atm_iv = capture_atm_iv_for_symbol(chain, spot=float(spot), today=deps.today)
        if atm_iv is None:
            log.info("iv_capture skip %s: no ATM pair in chain", symbol)
            continue
        _upsert_iv_for_today(deps.engine, symbol, atm_iv, deps.today)
        written += 1
    return written


def _upsert_iv_for_today(
    engine: Engine, symbol: str, atm_iv: float, day: dt.date,
) -> None:
    """At most one row per (symbol, calendar day). Replace if it exists.

    ``recorded_at`` is anchored to the captured ``day`` (12:00 UTC) rather
    than wall-clock now() so the (symbol, date) idempotency holds even
    when ``day`` is back-dated for tests or replays. Production callers
    pass today=date.today(), so the anchor lands on today as expected.
    """
    day_start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    day_end = day_start + dt.timedelta(days=1)
    # Anchor the new row at noon of the captured day. The pre-existing
    # query/dashboard reads use ``recorded_at`` for "most recent IV by
    # date" — same-day comparisons work either way.
    recorded_at = day_start + dt.timedelta(hours=12)
    with Session(engine) as s:
        existing = s.query(OptionIvHistory).filter(
            and_(
                OptionIvHistory.symbol == symbol,
                OptionIvHistory.recorded_at >= day_start,
                OptionIvHistory.recorded_at < day_end,
            )
        ).all()
        for row in existing:
            s.delete(row)
        s.add(OptionIvHistory(
            symbol=symbol,
            recorded_at=recorded_at,
            atm_iv_30d=atm_iv,
        ))
        s.commit()
