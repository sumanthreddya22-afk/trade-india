"""Historical backtest harness.

Replays existing strategy + risk_manager + orchestrator code paths against
cached daily bars, generating synthetic closed trades + per-strategy/per-regime
metrics. The whole point is validation: every threshold in the live bot is
currently a guess, and this is the first tool that gives us empirical edge.

Modules:
- `bar_store` — SQLite-backed daily-bar cache.
- `simulator` — day-by-day replay loop.
- `metrics` — per-strategy/per-regime aggregation.
- `reporter` — markdown report writer.

Import directly from the submodule. The __init__ is intentionally empty so
partially-built features don't break test collection.
"""
