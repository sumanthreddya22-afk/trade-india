from decimal import Decimal
import pandas as pd

from trading_bot.universe import LiquidAsset
from trading_bot.screener import RankedCandidate, score_candidate, build_stage1_shortlist


def _asset(symbol):
    return LiquidAsset(
        symbol=symbol, name=symbol, asset_class="us_equity",
        exchange="NASDAQ", last_price=Decimal("100"),
        avg_dollar_volume=Decimal("1e9"), fractionable=True,
        sector_tags=("ai",),
    )


def test_score_candidate_combines_momentum_volume_and_sector():
    bars = pd.DataFrame({
        "close":  [100, 101, 102, 103, 104, 105],
        "volume": [1e6,  1.1e6, 1.2e6, 1.3e6, 1.5e6, 2e6],
    })
    benchmark_5d_pct = 0.5  # SPY up 0.5% over the same window
    cand = score_candidate(_asset("NVDA"), bars=bars, benchmark_5d_pct=benchmark_5d_pct)
    assert isinstance(cand, RankedCandidate)
    assert cand.symbol == "NVDA"
    # 5d return = (105-100)/100 = 5%, relative to benchmark = +4.5%
    assert cand.relative_5d_pct > 4.0
    # Volume ratio = last vol / mean(prior vols), well above 1.0
    assert cand.volume_ratio > 1.0
    assert cand.score > 0


from collections.abc import Callable


def _make_bars(close_path: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "close": close_path,
        "volume": [1_000_000] * len(close_path),
    })


def test_build_stage1_shortlist_ranks_by_score_and_caps_size():
    universe = [
        _asset("AAA"),
        _asset("BBB"),
        _asset("CCC"),
    ]
    bar_paths = {
        "AAA": [100, 100, 100, 100, 100, 110],  # +10% 5d
        "BBB": [100, 100, 100, 100, 100, 105],  # +5% 5d
        "CCC": [100, 100, 100, 100, 100, 101],  # +1% 5d
        "SPY": [100, 100, 100, 100, 100, 100],  # 0% 5d
    }

    def loader(sym: str) -> pd.DataFrame:
        return _make_bars(bar_paths[sym])

    short = build_stage1_shortlist(universe, bar_loader=loader, top_n=2, benchmark_symbol="SPY")
    assert [c.symbol for c in short] == ["AAA", "BBB"]


def test_build_stage1_shortlist_handles_missing_bars():
    universe = [_asset("AAA"), _asset("EMPTY")]

    def loader(sym: str) -> pd.DataFrame:
        if sym == "EMPTY":
            return pd.DataFrame()
        return _make_bars([100, 100, 100, 100, 100, 110])

    short = build_stage1_shortlist(universe, bar_loader=loader, top_n=10, benchmark_symbol="SPY")
    assert "EMPTY" not in {c.symbol for c in short}
