# `registry/` — L4 Strategy Registry

**Status:** Empty skeleton — populated **Phase 4**.

## Mandate (Plan v4 §3)

Authoritative list of strategy versions and their validation artifacts.
Append-only via new version row; `INSERT` requires a complete promotion packet.

## Schema (lands Phase 4)

- `registry/strategies` (SQLite). Columns include `code_hash`, `config_hash`,
  `hypothesis_id`, `validation_artifact_id`, `lane`, `expiry_date`, `owner`.
- `registry/validation_artifacts` — one row per Tier-1/2/3 promotion gate
  pass. The artifact ID is referenced by the strategy row.

## Hard rules

- `INSERT` requires all hash fields to point to real artifacts on disk.
- Updates are forbidden; a new version row supersedes the prior.
- Expiry: paper-candidate and live-candidate rows expire after 90 days unless
  re-validated.

## What does NOT go here

- Strategy code itself (lives under a versioned `strategies/` directory not
  yet created — comes with Phase 4).
- The seed thesis (lives at `docs/edge_thesis_v1.md`).
