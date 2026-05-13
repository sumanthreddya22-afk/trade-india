# `risk/` — L6 Risk Kernel

**Status:** Empty skeleton — populated **Phase 2**.

## Mandate (Plan v4 §6)

Final veto. Every intent emitted by the kernel passes through `risk/precheck`
before reaching `execution/`. **Halts cannot be bypassed.** All decisions
logged to `strategy_decision.risk_decision`.

## Modules (lands Phase 2)

- `precheck.py` — single-entry gate; consults the lock files below.
- `account_caps.py` — daily/trailing drawdown, PDT (entry-side only, never
  blocks exits — see Plan §6 design notes).
- `lane_caps.py` — per-lane allocation and daily-loss limits.
- `symbol_caps.py` — per-symbol gross caps.
- `kill_switches.py` — the eight kill conditions from §6 (recon mismatch,
  unknown position older than 15 min, stale data, lock hash mismatch, broker
  API error rate, clock skew, integrity_check, intraday PnL floor).
- `halt_router.py` — translates a kill into a state transition the kernel
  honours next tick.

## Locks consulted (loaded once at boot, hash-verified)

- `policy/risk_policy.lock`
- `policy/pdt_policy.lock`
- `policy/lane_caps.lock`
- `policy/data_freshness.lock`
- `policy/short_policy.lock`

## What does NOT go here

- LLM calls (forbidden by every persona's `forbidden_actions`).
- Order submission (lives in `execution/`).
- Cost model (lives in `execution/cost_model.py`, parameters in
  `policy/cost_model.lock`).
