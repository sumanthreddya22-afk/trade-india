"""Operator-facing controls — single source of truth for the v4 control
surface, used by both the CLI (`bot <command>`) and the dashboard
(localhost FastAPI).

Every control here is a thin orchestrator on top of existing kernel /
registry / risk modules; nothing decides risk policy on its own. The
controls module exists so the dashboard and CLI never duplicate logic.

Categories:
  * **halt / resume** — manual_operator_halt kill switch.
  * **risk-profile** — three presets (safe / neutral / aggressive) that
    rewrite ``policy/risk_policy.lock`` and regenerate ``policy/HASHES``.
    Loosening still respects the 7-day cooldown.
  * **strategy** — list registered strategies; submit a new hypothesis
    (three modes: draft, intake, mutate).
  * **status** — one-shot snapshot used by the dashboard front page.
"""
from __future__ import annotations

from trading_bot.operator import controls  # noqa: F401

__all__ = ["controls"]
