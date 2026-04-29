"""Tests for the daily IV capture job — the only place we mass-fetch chains.
Bounded to the eligible set (allowlist - blocklist), one chain per symbol per day."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trading_bot.options.chain import ChainContract
from trading_bot.options.iv_capture import run_iv_capture, IvCaptureDeps
from trading_bot.state_db import Base, OptionIvHistory


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'cap.db'}")
    Base.metadata.create_all(e)
    return e


def _atm_pair(spot: float, dte: int, iv_call: float, iv_put: float, today: dt.date):
    exp = today + dt.timedelta(days=dte)
    call = ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}C{int(spot*1000):08d}",
        underlying="AAPL", expiration=exp, kind="C", strike=spot,
        bid=2.0, ask=2.10, last=2.05, volume=10, open_interest=100,
        implied_volatility=iv_call, delta=0.50,
    )
    put = ChainContract(
        contract_symbol=f"AAPL{exp:%y%m%d}P{int(spot*1000):08d}",
        underlying="AAPL", expiration=exp, kind="P", strike=spot,
        bid=2.0, ask=2.10, last=2.05, volume=10, open_interest=100,
        implied_volatility=iv_put, delta=-0.50,
    )
    return [call, put]


def test_iv_capture_writes_atm_iv_for_each_symbol(engine):
    today = dt.date(2026, 4, 29)
    chain_aapl = _atm_pair(spot=200.0, dte=30, iv_call=0.28, iv_put=0.30, today=today)
    chain_msft = _atm_pair(spot=400.0, dte=30, iv_call=0.20, iv_put=0.22, today=today)
    opt = MagicMock()
    opt.get_chain.side_effect = lambda underlying, **kw: {
        "AAPL": chain_aapl, "MSFT": chain_msft,
    }[underlying]
    deps = IvCaptureDeps(
        option_alpaca=opt, engine=engine,
        spot_for=lambda s: {"AAPL": 200.0, "MSFT": 400.0}[s],
        eligible={"AAPL", "MSFT"}, today=today,
    )
    written = run_iv_capture(deps)
    assert written == 2
    with Session(engine) as s:
        rows = sorted(s.query(OptionIvHistory).all(), key=lambda r: r.symbol)
    assert [r.symbol for r in rows] == ["AAPL", "MSFT"]
    assert rows[0].atm_iv_30d == pytest.approx(0.29, abs=1e-6)  # (0.28 + 0.30) / 2
    assert rows[1].atm_iv_30d == pytest.approx(0.21, abs=1e-6)


def test_iv_capture_skips_symbol_without_spot(engine):
    today = dt.date(2026, 4, 29)
    opt = MagicMock(); opt.get_chain.return_value = []
    deps = IvCaptureDeps(
        option_alpaca=opt, engine=engine,
        spot_for=lambda s: None,  # no spot data → can't compute ATM
        eligible={"AAPL"}, today=today,
    )
    assert run_iv_capture(deps) == 0
    with Session(engine) as s:
        assert s.query(OptionIvHistory).count() == 0


def test_iv_capture_handles_chain_fetch_failure(engine):
    today = dt.date(2026, 4, 29)
    opt = MagicMock()
    opt.get_chain.side_effect = Exception("rate limit")
    deps = IvCaptureDeps(
        option_alpaca=opt, engine=engine,
        spot_for=lambda s: 200.0,
        eligible={"AAPL"}, today=today,
    )
    # Fault-tolerant — the failure is logged but the function returns 0 written
    assert run_iv_capture(deps) == 0


def test_iv_capture_skips_symbol_with_no_atm_pair(engine):
    today = dt.date(2026, 4, 29)
    opt = MagicMock()
    opt.get_chain.return_value = []  # empty chain
    deps = IvCaptureDeps(
        option_alpaca=opt, engine=engine,
        spot_for=lambda s: 200.0,
        eligible={"AAPL"}, today=today,
    )
    assert run_iv_capture(deps) == 0


def test_iv_capture_idempotent_same_day(engine):
    """Running twice on the same day produces a single row per symbol
    (overwrite-by-day, not append-per-call) so re-running doesn't bloat history."""
    today = dt.date(2026, 4, 29)
    chain = _atm_pair(spot=200.0, dte=30, iv_call=0.28, iv_put=0.30, today=today)
    opt = MagicMock(); opt.get_chain.return_value = chain
    deps = IvCaptureDeps(
        option_alpaca=opt, engine=engine,
        spot_for=lambda s: 200.0,
        eligible={"AAPL"}, today=today,
    )
    run_iv_capture(deps)
    run_iv_capture(deps)
    with Session(engine) as s:
        # Two rows allowed if recorded_at differs across runs (it does — but
        # both fall on the same date). The "idempotent same day" guarantee:
        # at most one row PER (symbol, calendar date).
        rows = s.query(OptionIvHistory).filter(OptionIvHistory.symbol == "AAPL").all()
    same_day = [r for r in rows if r.recorded_at.date() == today]
    assert len(same_day) == 1
