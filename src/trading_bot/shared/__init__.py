"""Shared foundation utilities used by all three pipelines (stocks, crypto, options).

Members:
- alpaca_client: one broker connection (asset-class branches at API call boundary)
- risk_manager: portfolio-wide concentration limits (must be global)
- state_db: connection + base classes only (tables defined per-pipeline)
- daemon: scheduler + role registry (one process loop)
- submit_txn: optimistic concurrency transactional submit helper
- llm_transport: Claude CLI subprocess + model router + batching + cache
- audit: per-call cost tracking, persona logging, drift reports
- config: settings loader (shared + per-pipeline sections)
- personas/: cross-cutting personas (drift detector, PR bot, audit lead)
- automation/: pre-commit hook, drift detector agent, CI lint, PR-bot
"""
