# ADR 0001: Three independent trading pipelines on a thin shared layer

- **Date:** 2026-05-02
- **Status:** accepted
- **Pipelines affected:** shared, stocks, crypto, options
- **Author:** operator

## Context

The stocks workflow shipped a 7-phase upgrade (diversified news, scout
debate, hold debate, lesson loop, adaptive thresholds, circuit breakers
+ adversarial defense, event-driven ingestion). Expanding to crypto and
options forced a choice between four architectures:

1. Engine + three pipeline folders (shared engine API)
2. Three fully independent stacks
3. Shared infra with `asset_class` parameter (current state)
4. Hybrid (stocks shared + crypto/options independent)

Three modules in the current shared design were silently leaking across
asset classes (`debate_lessons`, `circuit_breaker_events`,
`threshold_overrides` lacked `asset_class`). Solo-developer ergonomics
and the wheel state machine for options pointed away from shared
abstractions.

## Decision

Three fully independent pipelines (`pipelines/stocks/`,
`pipelines/crypto/`, `pipelines/options/`) on a thin `shared/` layer.
Each pipeline owns its debates, ingest, lesson loop, breaker, streamer,
personas, regime, config, and DB tables. `shared/` only carries cross-
asset utilities that genuinely cannot be duplicated: broker connection,
portfolio-wide risk manager, DB connection, daemon scheduler,
optimistic-concurrency submit helper, LLM transport, audit, config
loader.

## Consequences

- Each pipeline reads as a self-contained system; zero cross-asset
  blast radius when editing one.
- Bug fixes and improvements apply ~2× (crypto + options share most
  patterns; stocks moves at its own cadence). Mitigated by automated
  cross-pollination tooling (pre-commit hook, weekly drift detector,
  PR-bot, CI lint, quarterly audit).
- Code volume ~2.5–3× the equivalent shared-infra design.
- Schema fragmentation across pipelines is intentional; each owns
  `*_stock`, `*_crypto`, `*_options` tables.
- The wheel state machine in options gets first-class modeling without
  bending shared abstractions.

## Alternatives considered

- Option 1 (shared engine) was rejected because the engine API has to be
  designed correctly up-front and a bug in the engine still affects all
  three pipelines.
- Option 3 (current shared infra) was rejected because cognitive
  overhead per file scales with all three asset classes simultaneously.
- Option 4 (hybrid) was the runner-up — fastest to first crypto trade,
  smallest disruption — but the user prioritized architectural
  uniformity across the three asset classes over the ~3-4 week savings.
