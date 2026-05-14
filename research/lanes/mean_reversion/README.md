# Phase 7 — Mean Reversion lane (SCAFFOLD)

This directory is the second strategy lane scaffold. **Nothing in here trades
yet.** Promotion requires you to:

1. Fill in `edge_thesis_v2.md` with the operator-authored thesis.
2. Fill in `search_space_v2.json` with mutation parameters.
3. Add both file hashes to `policy/HASHES` via `python tools/recompute_hashes.py`.
4. Register the strategy via `bot strategy submit --mode draft` or
   `tools/register_seed_strategy.py` (modify for v2).
5. Run the research factory; produce a Tier-1 validation artifact.
6. Promote to `shadow` via `bot strategy ...` (Phase 4 promotion gate).

## Files in this scaffold

- `edge_thesis_v2.md.template` — fill in: hypothesis, mechanism, expected
  regimes, kill criteria, falsification plan.
- `search_space_v2.json.template` — fill in: lookback, z-score thresholds,
  entry / exit triggers, universe restriction.

## Why mean reversion?

Plan v4 §2 allows multiple research hypotheses *in parallel in the sandbox*
but only **one thesis at a time in the production registry**. Mean reversion
is a common, well-studied complement to momentum (the seed thesis), and
it has different regime sensitivity, so a portfolio of the two can be
more stable than either alone.

The operator decides whether to keep this scaffold or replace it with a
different second-lane idea (crypto trend, low-vol carry, etc.).
