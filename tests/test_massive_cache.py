from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from trading_bot.massive_cache import MassiveGroupedCache


def _df(rows: list[tuple[str, float, float, float, float, float, float]]) -> pd.DataFrame:
    """Build a grouped DataFrame indexed by ticker with columns o,h,l,c,v,vw."""
    return pd.DataFrame(
        [{"o": o, "h": h, "l": l, "c": c, "v": v, "vw": vw} for (_, o, h, l, c, v, vw) in rows],
        index=[r[0] for r in rows],
    )


def test_store_and_has(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    d = date(2026, 4, 24)
    c.store(d, _df([("AAPL", 1, 2, 0.5, 1.5, 1000, 1.4)]))
    assert c.has(d) is True
    assert c.has(date(2026, 4, 23)) is False


def test_store_is_idempotent(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    d = date(2026, 4, 24)
    c.store(d, _df([("AAPL", 1, 2, 0.5, 1.5, 1000, 1.4)]))
    c.store(d, _df([("AAPL", 9, 9, 9, 9, 9, 9)]))
    out = c.latest(max_age_days=30)
    assert out is not None
    on_date, df = out
    assert on_date == d
    assert float(df.loc["AAPL", "c"]) == 9.0
    assert len(df) == 1


def test_latest_returns_most_recent_within_window(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=10), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    c.store(today - timedelta(days=2), _df([("NEW", 2, 2, 2, 2, 2, 2)]))
    out = c.latest(max_age_days=5)
    assert out is not None
    on_date, df = out
    assert on_date == today - timedelta(days=2)
    assert "NEW" in df.index
    assert "OLD" not in df.index


def test_latest_none_when_no_fresh_data(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=10), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    assert c.latest(max_age_days=5) is None


def test_latest_none_on_empty_cache(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    assert c.latest(max_age_days=30) is None


def test_evict_older_than(tmp_path):
    c = MassiveGroupedCache(tmp_path / "g.db")
    today = datetime.now(timezone.utc).date()
    c.store(today - timedelta(days=40), _df([("OLD", 1, 1, 1, 1, 1, 1)]))
    c.store(today - timedelta(days=2), _df([("NEW", 2, 2, 2, 2, 2, 2)]))
    c.evict_older_than(days=30)
    assert c.has(today - timedelta(days=40)) is False
    assert c.has(today - timedelta(days=2)) is True


def test_store_empty_dataframe_is_noop(tmp_path):
    """Massive returns empty results on holidays/weekends — cache should not error."""
    c = MassiveGroupedCache(tmp_path / "g.db")
    c.store(date(2026, 4, 25), pd.DataFrame())
    assert c.has(date(2026, 4, 25)) is False
