from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd

from trading_bot.backtest.bar_store import BarStore


def _df(dates_closes: list[tuple[date, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"open": c, "high": c * 1.01, "low": c * 0.99,
             "close": c, "volume": 1_000_000}
            for _, c in dates_closes
        ],
        index=pd.DatetimeIndex(
            [pd.Timestamp(d) for d, _ in dates_closes], name="timestamp"
        ),
    )


def test_empty_store_returns_empty(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    out = s.get("SPY", end_date=date(2024, 1, 10), lookback_days=20)
    assert out.empty


def test_warm_then_get_roundtrip_preserves_ohlcv(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([
        (date(2024, 1, 2), 470.0),
        (date(2024, 1, 3), 471.5),
        (date(2024, 1, 4), 469.0),
    ])
    inserted = s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 4), market=market)
    assert inserted["SPY"] == 3

    out = s.get("SPY", end_date=date(2024, 1, 4), lookback_days=10)
    assert len(out) == 3
    assert float(out["close"].iloc[-1]) == 469.0
    assert float(out["high"].iloc[0]) == 470.0 * 1.01


def test_get_respects_end_date_no_future_leak(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([
        (date(2024, 1, d), 100 + d) for d in range(2, 11)
    ])
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 10), market=market)

    out = s.get("SPY", end_date=date(2024, 1, 5), lookback_days=10)
    last_dates = [pd.Timestamp(d).date() for d in out.index]
    assert max(last_dates) == date(2024, 1, 5)
    assert date(2024, 1, 8) not in last_dates  # no leak past end_date


def test_warm_is_idempotent_no_duplicates(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([(date(2024, 1, 2), 100.0)])
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 2), market=market)
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 2), market=market, refresh=True)

    out = s.get("SPY", end_date=date(2024, 1, 2), lookback_days=5)
    assert len(out) == 1


def test_is_warm_false_when_empty(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    assert not s.is_warm("SPY", from_date=date(2024, 1, 1), to_date=date(2024, 1, 31))


def test_is_warm_true_after_warm(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([(date(2024, 1, 2), 100.0)])
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 2), market=market)
    assert s.is_warm("SPY", from_date=date(2024, 1, 2), to_date=date(2024, 1, 2))


def test_get_bar_exact_date(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([
        (date(2024, 1, 2), 100.0),
        (date(2024, 1, 3), 101.0),
    ])
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 3), market=market)

    bar = s.get_bar("SPY", date(2024, 1, 3))
    assert bar is not None
    assert bar.close == 101.0
    assert s.get_bar("SPY", date(2024, 1, 4)) is None


def test_trading_dates_returns_ascending_range(tmp_path):
    s = BarStore(tmp_path / "bars.db")
    market = MagicMock()
    market.get_daily_bars.return_value = _df([
        (date(2024, 1, 2), 1.0),
        (date(2024, 1, 3), 2.0),
        (date(2024, 1, 5), 3.0),
        (date(2024, 1, 8), 4.0),
    ])
    s.warm(["SPY"], from_date=date(2024, 1, 2), to_date=date(2024, 1, 8), market=market)

    dates = s.trading_dates("SPY", from_date=date(2024, 1, 1), to_date=date(2024, 1, 10))
    assert dates == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 5), date(2024, 1, 8)]
