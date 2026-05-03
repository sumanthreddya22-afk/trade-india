"""Options intel sources (Phase 3).

Each adapter pulls one external source and writes ``IntelEventOptions``
rows that the options aggregator rolls up into ``IntelCandidateOptions``
for the scout debate to read.

Sources implemented (Phase 3 baseline):
  - earnings_calendar  (yfinance) — upcoming earnings per underlying
  - cboe_skew          (FRED VIX/SKEW) — index-level skew snapshot
  - unusual_options_flow — placeholder for Phase 3+ paid feed integration

Adapters share the ``poll_*(engine, *, ..., now=None) -> SourceResult``
shape mirrored from the crypto sources framework.
"""
from __future__ import annotations
