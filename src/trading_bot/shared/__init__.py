"""Shared utility modules that survived the v4 cleanup.

Members:
- black_scholes: closed-form option pricing (used by Wheel + ingest)
- tz: US/Eastern + UTC helpers used by operator/CLI + daemon

The legacy ``alpaca_client``/``config``/``submit_txn`` modules and the
``shared.personas``/``llm_transport``/``audit``/``automation`` packages
were removed in Phase 11 — the v4 kernel routes broker calls through
``ingest.alpaca_adapter``, settings through ``.env``, and order
submission through ``execution.order_router``.
"""
