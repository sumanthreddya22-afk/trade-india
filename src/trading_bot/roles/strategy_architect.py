"""Strategy Architect — Tier 5 lab role (Role 18).

Saturday weekly: asks Claude to propose 1-3 strategy templates as Python
modules conforming to MomentumStrategy's evaluate(...) signature. Output
stored in TemplateProposal rows for Code Reviewer.

Boots disabled if ANTHROPIC_API_KEY is missing — logs a skip event.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from sqlalchemy.orm import Session

from trading_bot.anthropic_client import (
    AnthropicClient,
    AnthropicCredsMissingError,
    BudgetExceededError,
    default_architect_model,
)
from trading_bot.leaderboard import top_n
from trading_bot.roles.runner import BaseRole
from trading_bot.state_db import RoleRun, TemplateProposal


SYSTEM_PROMPT = """You are the Strategy Architect of an autonomous trading system. \
Your job is to propose 1-3 new strategy templates as Python modules conforming \
to a momentum-style evaluate(symbol, ind, equity) -> Signal signature.

YOUR INPUTS (attached below):
1. Top 10 leaderboard variants and their fitness.
2. Brief description of the active config.

YOUR OUTPUT FORMAT (STRICT JSON, no prose outside the JSON):
[
  {
    "name": "snake_case_name_v1",
    "rationale": "1-2 sentences: WHAT regime/inefficiency this exploits.",
    "expected_regime": "trending_up|sideways|volatile_bear|mean_reverting",
    "code": "<full Python module text>",
    "tests": "<full pytest module text>",
    "params_to_search": {"param_name": [low, high, "int|float"]}
  }
]

HARD CONSTRAINTS ON YOUR PROPOSED CODE:
- Imports allowed: pandas, numpy, ta, math, datetime, dataclasses, typing, decimal, enum.
- Imports prohibited: os, sys, subprocess, requests, urllib, eval, exec, __import__, open.
- No I/O of any kind. No file reads. No network calls.
- Must implement evaluate(symbol: str, ind, equity) -> Signal returning trading_bot.strategy.Signal.
- Must include from_params(cls, params: dict) classmethod.
- Must NOT use future bars. Indicators must use only data <= current bar.
- Must run a 5-year backtest in under 30 seconds. Tests must complete in < 30s.
"""


class StrategyArchitectRole(BaseRole):
    name = "strategy_architect"
    tier = 5
    process = "lab"
    job_description = (
        "Weekly Anthropic call: propose 1-3 strategy templates conforming "
        "to MomentumStrategy's evaluate signature. Output stored as "
        "TemplateProposal rows for Code Reviewer."
    )
    sla_seconds = 5 * 60
    upstream_roles: list[str] = []
    downstream_roles = ["code_reviewer"]

    def _do_work(self, ctx):
        try:
            client = AnthropicClient(
                role_name=self.name, model=default_architect_model(), engine=self.engine
            )
        except AnthropicCredsMissingError:
            return {"skipped": True, "reason": "no_anthropic_creds"}

        # Build context: top leaderboard rows
        with Session(self.engine) as s:
            top = top_n(s, n=10)
        leaderboard_summary = [
            {
                "template": r.template_name,
                "alpha_vs_spy_x": r.alpha_vs_spy_x,
                "sortino": r.sortino,
                "max_dd_pct": r.max_dd_pct,
                "fitness_score": r.fitness_score,
            }
            for r in top
        ]
        user_msg = json.dumps(
            {
                "leaderboard_top10": leaderboard_summary,
                "active_template_hint": "momentum (existing). Propose templates for other regimes.",
            },
            indent=2,
        )

        try:
            resp = client.complete(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=8000,
            )
        except BudgetExceededError as e:
            return {"skipped": True, "reason": "anthropic_budget_exceeded", "error": str(e)}
        except AnthropicCredsMissingError:
            return {"skipped": True, "reason": "no_anthropic_creds"}

        proposals = _parse_proposals(resp.text)
        names: list[str] = []
        with Session(self.engine) as session:
            for p in proposals:
                session.add(
                    TemplateProposal(
                        proposed_at=dt.datetime.now(dt.timezone.utc),
                        name=p["name"],
                        rationale=p["rationale"],
                        expected_regime=p.get("expected_regime", "unknown"),
                        code=p["code"],
                        tests=p["tests"],
                        params_to_search_json=json.dumps(p.get("params_to_search", {})),
                        review_status="pending",
                    )
                )
                names.append(p["name"])
            session.commit()

        return {
            "n_proposals": len(proposals),
            "names": names,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
        }

    def _kpi_value(self, lookback_days: int) -> tuple[str, float, str]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
        with Session(self.engine) as session:
            count = (
                session.query(RoleRun)
                .filter(RoleRun.role_name == self.name, RoleRun.started_at >= cutoff)
                .count()
            )
            proposals = (
                session.query(TemplateProposal)
                .filter(TemplateProposal.proposed_at >= cutoff)
                .count()
            )
        return (
            "proposals_per_run",
            float(proposals) / max(count, 1),
            f"{proposals} proposals / {count} runs in last {lookback_days}d",
        )


def _parse_proposals(text: str) -> list[dict]:
    """Extract the JSON array from Claude's response, tolerating ```json fences."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"^```\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON array within the text
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    # Validate each entry has required keys
    valid = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if all(k in item for k in ("name", "code", "tests", "rationale")):
            valid.append(item)
    return valid
