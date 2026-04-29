import datetime as dt
import pytest
from trading_bot.options.symbols import parse_occ, format_occ, OccContract


def test_parse_aapl_call():
    c = parse_occ("AAPL250117C00190000")
    assert c == OccContract(underlying="AAPL", expiration=dt.date(2025, 1, 17),
                            kind="C", strike=190.0)


def test_parse_spy_put_with_decimal_strike():
    c = parse_occ("SPY250516P00425500")
    assert c.strike == 425.5
    assert c.kind == "P"


def test_format_round_trip():
    c = OccContract(underlying="QQQ", expiration=dt.date(2026, 6, 19),
                    kind="C", strike=505.0)
    assert format_occ(c) == "QQQ260619C00505000"


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_occ("nope")
