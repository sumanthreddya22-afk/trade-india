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
