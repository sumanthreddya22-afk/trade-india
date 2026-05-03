# Architecture Decision Records

Lightweight one-paragraph notes capturing non-obvious design decisions that
should outlive the conversations that produced them.

## When to write an ADR

Add an ADR when you make a design choice that:

- Diverges deliberately between pipelines (e.g. crypto uses 10-min hold
  cadence, stocks uses 15-min — write down *why*).
- Picks one of several plausible approaches (e.g. contract-level locks vs.
  symbol-level locks for options — say *why* contract level wins).
- Embeds a non-obvious constraint (rate limits, broker quirks, regulatory
  requirements).
- Was hard-won — discovered the hard way after a bug, an outage, or a
  surprising trade outcome.

Skip ADRs for purely-mechanical choices that any reader would derive from
the code.

## File naming

`NNNN-short-title-in-kebab-case.md`

Numbers are sequential. Once an ADR is written, it is immutable
(future ADRs can supersede it; never edit the original except for typos).

## File template

```markdown
# ADR NNNN: <decision title>

- **Date:** YYYY-MM-DD
- **Status:** accepted | superseded by ADR XXXX | deprecated
- **Pipelines affected:** stocks | crypto | options | shared
- **Author:** <name or role>

## Context

What's the situation. What problem are we solving.

## Decision

The choice we made, in one or two sentences.

## Consequences

What this implies for the code, tests, operations, future work.
What we accept by choosing this path.

## Alternatives considered

Brief note on what we did not pick and why.
```

## Discovery aids

- The cross-pollination drift detector reads this directory weekly to
  understand which divergences across pipelines are deliberate.
- The CI lint will warn (not block) when a new config key is added to
  `strategy/config.yaml` without a matching ADR.
