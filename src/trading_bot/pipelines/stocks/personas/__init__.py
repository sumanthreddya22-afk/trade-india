"""Stocks-pipeline personas (Phase 2 strangler-fig).

Each module here is a thin re-export of the legacy
``trading_bot.personas.<name>`` module. The PERSONA dict, VERSION,
and PROMPT constants are forwarded so:

  - ``persona_module.PERSONA`` works for the /desk roster discoverer.
  - ``persona_module.PROMPT`` / ``.VERSION`` works for the legacy
    debate code that reads those constants directly.

Why shims instead of moving the source: the legacy paths are imported
from a dozen call sites (hold_debate, intel/scout_debate,
lesson_loop, debate_outcome_analyzer, …). Migrating them all in one
commit would produce a large blast radius. Shims let the canonical
namespace exist immediately while real migration happens at the
caller's pace.
"""
from __future__ import annotations
