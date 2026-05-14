"""Operator UI — FastAPI dashboard for v4.

Single localhost service that exposes:
  * status page (positions, kill switches, heartbeats, strategies)
  * halt / resume buttons
  * risk profile selector (safe / neutral / aggressive)
  * strategy submission form (draft / intake / mutate)

The UI is intentionally minimal — one HTML page, no JS framework, no
build step. The dashboard runs on the same machine as the daemon and
serves localhost only by default. For remote access the operator runs
the daemon under a reverse proxy with auth.
"""
from __future__ import annotations

from trading_bot.operator_ui.app import app  # noqa: F401

__all__ = ["app"]
