from decimal import Decimal

import pandas as pd

from trading_bot.screener import RankedCandidate
from trading_bot.strategy_lanes import Lane, LaneCandidate


def _ranked(symbol: str) -> RankedCandidate:
    return RankedCandidate(
        symbol=symbol, asset_class="us_equity", sector_tags=("ai",),
        last_price=Decimal("100"), one_day_return_pct=1.0,
        five_day_return_pct=5.0, relative_5d_pct=4.0, volume_ratio=1.5, score=10.0,
    )


class _PassThroughLane:
    name = "passthrough"

    def evaluate(self, ranked: list[RankedCandidate], bar_loader):
        return [
            LaneCandidate(
                symbol=c.symbol, lane=self.name, conviction=0.5,
                reason="passes through", source_score=c.score,
            )
            for c in ranked
        ]


def test_lane_protocol_accepts_passthrough():
    lane: Lane = _PassThroughLane()
    out = lane.evaluate([_ranked("NVDA"), _ranked("AMD")], bar_loader=lambda s: pd.DataFrame())
    assert len(out) == 2
    assert out[0].lane == "passthrough"
    assert 0.0 <= out[0].conviction <= 1.0


from trading_bot.strategy_lanes import MomentumLane


def _modest_uptrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """Alternating +0.6% / -0.4% — net up ~14% over 60d, RSI lands ~60."""
    closes = [start]
    for i in range(n - 1):
        change = 0.6 if i % 2 == 0 else -0.4
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def _modest_downtrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """Alternating -0.6% / +0.4% — net down, RSI lands ~40."""
    closes = [start]
    for i in range(n - 1):
        change = -0.6 if i % 2 == 0 else 0.4
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def _parabolic_uptrend(start: float = 100, n: int = 60) -> pd.DataFrame:
    """3 days +1.5% / 1 day -0.3% — RSI > 70, lane should reject as overbought."""
    closes = [start]
    for i in range(n - 1):
        change = 1.5 if i % 4 != 3 else -0.3
        closes.append(closes[-1] * (1 + change / 100))
    return pd.DataFrame({"close": closes, "volume": [1e6] * n})


def test_momentum_lane_accepts_modest_uptrend():
    lane = MomentumLane()
    bars = {"NVDA": _modest_uptrend()}
    cand = _ranked("NVDA")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert len(out) == 1
    assert out[0].symbol == "NVDA"
    assert out[0].lane == "momentum"
    assert out[0].conviction > 0


def test_momentum_lane_rejects_downtrend():
    lane = MomentumLane()
    bars = {"DOWN": _modest_downtrend()}
    cand = _ranked("DOWN")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert out == []


def test_momentum_lane_rejects_parabolic_overbought():
    lane = MomentumLane()
    bars = {"HOT": _parabolic_uptrend()}
    cand = _ranked("HOT")
    out = lane.evaluate([cand], bar_loader=lambda s: bars.get(s, pd.DataFrame()))
    assert out == []


from trading_bot.strategy_lanes import MeanReversionLane


def test_mean_reversion_lane_accepts_oversold_below_lower_band():
    lane = MeanReversionLane()
    # 30 days of stable price then a sharp drop
    closes = [100] * 25 + [90, 88, 86, 84, 82]
    bars = pd.DataFrame({"close": closes, "volume": [1e6] * len(closes)})
    cand = _ranked("DROP")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert len(out) == 1
    assert out[0].lane == "mean_reversion"


def test_mean_reversion_lane_rejects_normal_market():
    lane = MeanReversionLane()
    closes = [100 + i * 0.1 for i in range(30)]  # gentle uptrend, no oversold
    bars = pd.DataFrame({"close": closes, "volume": [1e6] * 30})
    cand = _ranked("NORMAL")
    out = lane.evaluate([cand], bar_loader=lambda s: bars)
    assert out == []
