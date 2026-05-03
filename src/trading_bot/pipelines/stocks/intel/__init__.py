"""Stocks-pipeline intel (Phase 2 strangler-fig).

Re-export shim layer. Each submodule forwards to the legacy
``trading_bot.intel.<name>`` module while the source files remain in
their original location during the transition. Once all callers
migrate to the new namespace, the legacy modules can be flipped to
re-exports themselves and eventually removed.
"""
from __future__ import annotations

from trading_bot.intel import (  # noqa: F401
    aggregator,
    adversarial,
    pool,
    scout_debate,
    sec_cik_map,
    sources,
)
