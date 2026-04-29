import datetime as dt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from trading_bot.options.iv_rank import compute_iv_rank, capture_atm_iv_for_symbol
from trading_bot.options.chain import ChainContract
from trading_bot.state_db import Base, OptionIvHistory


def _seed_history(engine, symbol, ivs: list[float]) -> None:
    today = dt.datetime.now(dt.timezone.utc)
    with Session(engine) as s:
        for i, iv in enumerate(ivs):
            s.add(OptionIvHistory(symbol=symbol,
                                  recorded_at=today - dt.timedelta(days=len(ivs) - i),
                                  atm_iv_30d=iv))
        s.commit()


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'iv.db'}")
    Base.metadata.create_all(e)
    return e


def test_iv_rank_high_when_current_above_history(engine):
    _seed_history(engine, "AAPL", [0.20, 0.22, 0.21, 0.23, 0.25])
    rank = compute_iv_rank(engine, "AAPL", current_iv=0.40)
    assert rank == 100.0  # current way above hi=0.25


def test_iv_rank_low_when_current_below_history(engine):
    _seed_history(engine, "AAPL", [0.20, 0.22, 0.21, 0.23, 0.25])
    rank = compute_iv_rank(engine, "AAPL", current_iv=0.10)
    assert rank == 0.0


def test_iv_rank_returns_none_when_history_too_short(engine):
    _seed_history(engine, "AAPL", [0.25, 0.27])  # < 30 entries
    assert compute_iv_rank(engine, "AAPL", current_iv=0.30, min_history=30) is None


def test_capture_atm_iv_picks_30dte_atm():
    today = dt.date(2026, 4, 28)
    chain = [
        ChainContract(contract_symbol="AAPL260530C00200000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="C", strike=200,
                      bid=1, ask=1.1, last=1.05, volume=10, open_interest=100,
                      implied_volatility=0.28, delta=0.50),
        ChainContract(contract_symbol="AAPL260530P00200000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="P", strike=200,
                      bid=1, ask=1.1, last=1.05, volume=10, open_interest=100,
                      implied_volatility=0.30, delta=-0.50),
        ChainContract(contract_symbol="AAPL260530C00210000", underlying="AAPL",
                      expiration=dt.date(2026, 5, 30), kind="C", strike=210,
                      bid=0.5, ask=0.6, last=0.55, volume=5, open_interest=50,
                      implied_volatility=0.40, delta=0.20),
    ]
    iv = capture_atm_iv_for_symbol(chain, spot=200.0, today=today)
    assert iv == pytest.approx((0.28 + 0.30) / 2, rel=1e-6)
