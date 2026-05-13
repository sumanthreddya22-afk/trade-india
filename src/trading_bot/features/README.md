# `features/` — L2 Point-in-Time Feature Store

**Status:** Empty skeleton — populated **Phase 2**.

## Mandate (Plan v4 §3)

Every feature row has `as_of_ts <= now` at compute time. No lookahead joins
allowed — schema-enforced.

## Modules (lands Phase 2)

- `asof_store.py` — Parquet + DuckDB write/read primitives.
- `feature_registry.py` — table of registered features. Adding a feature
  requires a code_hash and a unit test that exercises both regimes.

## Hard rules

- All reads MUST specify an `as_of_ts`. The store refuses reads where
  `as_of_ts > now` (future leakage) or where the requested ts pre-dates
  the source's earliest `claimed_event_ts`.
- No mutable features. Updates create a new (feature_id, as_of_ts) row.
