# src/trading_bot/options/wheel_lane.py
"""WheelLane — the wheel's strategy lane. Single entry point: evaluate().

Same shape as the equity momentum lane:
    universe → per-symbol analytics → lane.evaluate → decision → risk → order

The lane has two stages internally:
  * `passes_preflight` — cheap gates (regime, VIX, sentiment, IV rank,
    earnings, WSB spike, cycle state). No chain fetch. Skips ~95% of
    symbols on most days, so the runner only fetches chains for surfaced
    candidates.
  * `evaluate` — full decision. Calls passes_preflight first, then picks
    a contract from the supplied chain.

Returns WheelDecision(action, contract, reason). Action ∈
{"open_csp", "open_cc", "skip"}. Reason is human-readable and gets
journaled / shown in alerts and the dashboard."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from trading_bot.config import WheelConfig
from trading_bot.intelligence_finnhub import FinnhubClient
from trading_bot.options.chain import (
    ChainContract, pick_csp_contract, pick_cc_contract,
)


@dataclass(frozen=True)
class WheelInputs:
    """Per-symbol snapshot of everything the lane consults. The runner
    builds this once per symbol per scan.

    Bucket C: ``apewisdom`` was removed — the WSB spike gate was dead
    (wallstreetbets_mentions() was never warmed for the wheel pipeline,
    so is_spike() always returned False). VIX floor/ceiling + IV rank
    floor already cover the volatility-avoidance intent from a more
    reliable angle.
    """
    symbol: str
    regime: str
    vix: float | None
    sentiment_score: float | None
    spot: float
    iv_rank: float | None
    finnhub: FinnhubClient
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

    def passes_preflight(self, inp: WheelInputs) -> str | None:
        """Cheap gates that don't need an option chain. Returns None if all
        gates pass (caller should fetch chain and call evaluate); returns
        a skip reason string otherwise."""
        if not self.cfg.enabled:
            return "wheel_disabled"
        # Cycle-state guard: only no-cycle or ASSIGNED phase can produce
        # a NEW entry. csp_open / cc_open are managed by run_wheel_manage.
        if inp.cycle is not None:
            phase = getattr(inp.cycle, "phase", None)
            if phase not in ("assigned",):
                return f"cycle_already_open ({phase})"
        if inp.regime not in ("trending_up", "sideways"):
            return f"regime={inp.regime}"
        if inp.vix is None or not (self.cfg.vix_floor <= inp.vix <= self.cfg.vix_ceiling):
            return f"vix={inp.vix}"
        if inp.sentiment_score is not None and inp.sentiment_score < self.cfg.sentiment_floor:
            return f"sentiment={inp.sentiment_score:.2f}"
        if inp.iv_rank is None or inp.iv_rank < self.cfg.iv_rank_floor:
            return f"iv_rank={inp.iv_rank}"
        # Earnings window = today .. today + dte_max + 2 (avoid binary gap
        # risk on a CSP whose expiration straddles an earnings print).
        end = inp.today + dt.timedelta(days=self.cfg.dte_max + 2)
        if inp.finnhub.has_earnings_in_window(inp.symbol, inp.today, end):
            return "earnings_in_window"
        return None  # all preflight checks passed

    def evaluate(self, inp: WheelInputs) -> WheelDecision:
        """Full lane evaluation. Calls preflight first, then picks a
        contract from the supplied chain. Returns a WheelDecision with
        action ∈ {open_csp, open_cc, skip} and a contract when applicable."""
        skip_reason = self.passes_preflight(inp)
        if skip_reason is not None:
            return WheelDecision("skip", None, skip_reason)

        if inp.cycle is None:
            pick = pick_csp_contract(inp.chain, cfg=self.cfg, today=inp.today)
            if pick is None:
                return WheelDecision("skip", None, "no_csp_contract_in_band")
            return WheelDecision(
                "open_csp", pick,
                f"IV rank {inp.iv_rank:.0f}, delta {pick.delta:.2f}, "
                f"DTE {(pick.expiration - inp.today).days}",
            )
        # cycle in 'assigned' phase ⇒ open CC
        if inp.cost_basis is None:
            return WheelDecision("skip", None, "no_cost_basis")
        pick = pick_cc_contract(inp.chain, cost_basis=inp.cost_basis,
                                cfg=self.cfg, today=inp.today)
        if pick is None:
            return WheelDecision("skip", None, "no_cc_contract_in_band")
        return WheelDecision(
            "open_cc", pick,
            f"CC strike {pick.strike} ≥ cost basis {inp.cost_basis:.2f}, "
            f"delta {pick.delta:.2f}",
        )
