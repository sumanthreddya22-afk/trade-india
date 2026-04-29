# src/trading_bot/options/wheel_lane.py
"""WheelLane — applies entry filters to a single (symbol, chain) and emits a
WheelDecision: open_csp / open_cc / skip with a reason."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.config import WheelConfig
from trading_bot.intelligence_apewisdom import ApeWisdomClient
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.chain import (
    ChainContract, pick_csp_contract, pick_cc_contract,
)


@dataclass(frozen=True)
class WheelInputs:
    symbol: str
    regime: str
    vix: float | None
    sentiment_score: float | None
    spot: float
    iv_rank: float | None
    finnhub: FinnhubClient
    apewisdom: ApeWisdomClient
    today: dt.date
    chain: list[ChainContract]
    cycle: object | None  # WheelCycle row when present
    cost_basis: float | None


@dataclass(frozen=True)
class WheelDecision:
    action: str  # "open_csp" | "open_cc" | "skip"
    contract: ChainContract | None
    reason: str


class WheelLane:
    name = "wheel"

    def __init__(self, cfg: WheelConfig) -> None:
        self.cfg = cfg

    def evaluate(self, inp: WheelInputs) -> WheelDecision:
        if not self.cfg.enabled:
            return WheelDecision("skip", None, "wheel_disabled")
        if inp.regime not in ("trending_up", "sideways"):
            return WheelDecision("skip", None, f"regime={inp.regime}")
        if inp.vix is None or not (self.cfg.vix_floor <= inp.vix <= self.cfg.vix_ceiling):
            return WheelDecision("skip", None, f"vix={inp.vix}")
        if inp.sentiment_score is not None and inp.sentiment_score < self.cfg.sentiment_floor:
            return WheelDecision("skip", None, f"sentiment={inp.sentiment_score:.2f}")
        if inp.iv_rank is None or inp.iv_rank < self.cfg.iv_rank_floor:
            return WheelDecision("skip", None, f"iv_rank={inp.iv_rank}")
        if inp.apewisdom.is_spike(inp.symbol, multiplier=self.cfg.wsb_spike_multiplier):
            return WheelDecision("skip", None, "wsb_spike")
        # earnings window = today .. today + dte_max + 2
        end = inp.today + dt.timedelta(days=self.cfg.dte_max + 2)
        if inp.finnhub.has_earnings_in_window(inp.symbol, inp.today, end):
            return WheelDecision("skip", None, "earnings_in_window")

        if inp.cycle is None:
            pick = pick_csp_contract(inp.chain, cfg=self.cfg, today=inp.today)
            if pick is None:
                return WheelDecision("skip", None, "no_csp_contract_in_band")
            return WheelDecision("open_csp", pick, "")
        # cycle in 'assigned' phase ⇒ open CC
        if inp.cost_basis is None:
            return WheelDecision("skip", None, "no_cost_basis")
        pick = pick_cc_contract(inp.chain, cost_basis=inp.cost_basis,
                                cfg=self.cfg, today=inp.today)
        if pick is None:
            return WheelDecision("skip", None, "no_cc_contract_in_band")
        return WheelDecision("open_cc", pick, "")
