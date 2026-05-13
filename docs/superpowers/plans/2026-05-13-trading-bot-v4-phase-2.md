# Trading Bot v4 — Phase 2 Implementation Plan

**Spec:** `docs/superpowers/specs/2026-05-13-trading-bot-v4-phase-2-design.md`
**Status:** Shipped 2026-05-13 (trading halted; no calendar pressure).

## What landed

### Policy locks (real numerics)

```
policy/risk_policy.lock         Plan §6 — every numeric in the limit table
policy/pdt_policy.lock          Plan §6 PDT note + FINRA Notice 25-XX tracking
policy/lane_caps.lock           Plan §7 — 6 lanes with status + demotion criteria
policy/cost_model.lock          Plan §9 — pessimistic-lens parameters (stocks/crypto/options)
policy/data_freshness.lock      Plan §6 — equity 300s, crypto 60s, options 60s
policy/validation_policy.lock   Plan §4 — research/paper/live tier thresholds + BH-FDR
policy/role_personas.lock       Plan §1A — persona path manifest
policy/source_reliability.lock  KEPT SKELETON (lands Phase 1.5)
policy/short_policy.lock        KEPT SKELETON (lands SHORT phase)
policy/HASHES                   regenerated; 18 entries
```

### Risk kernel

```
src/trading_bot/risk/
  __init__.py
  policy_loader.py            # load_policy / verify_policy_hashes / honor_cooldown
  limits.py                   # RiskLimits dataclasses (parse_risk_policy)
  types.py                    # AccountState / Position / RiskDecision
  account_caps.py             # daily DD / trailing DD / intraday floor
  asset_class_caps.py         # equity 80% / crypto 15% / options 30%
  lane_caps.py                # lane status + per-lane allocation + daily loss + demote
  strategy_caps.py            # per-strategy 30-day loss
  symbol_order_caps.py        # per-symbol (reduce-to-fit) + per-order at-risk
  pdt.py                      # entry-side PDT (exits always pass)
  kill_switches.py            # 8 detectors + kill_switch_event table + fire/clear
  halt_router.py              # active kills -> halt verdict
  precheck.py                 # single-entry orchestrator
```

### Kernel boot

```
src/trading_bot/kernel/
  __init__.py
  boot.py                     # run_boot_checks (integrity / schema / hashes / chain / active kills)

tools/boot_check.py           # CLI driver
```

### Tests

```
tests/test_phase2_policy_loader.py
tests/test_phase2_limits_account.py
tests/test_phase2_limits_asset_class.py
tests/test_phase2_limits_lane.py
tests/test_phase2_limits_symbol_order.py
tests/test_phase2_pdt.py
tests/test_phase2_kill_switches.py
tests/test_phase2_halt_router.py
tests/test_phase2_precheck_integration.py
tests/test_phase2_boot_check.py
```

**Total Phase 2 tests: 66. Combined Phase 0+1+2 suite: 173 green.**

## P0 / P1 acceptance items satisfied

- ✓ Three policy lock files + HASHES (validation, risk, pdt populated; cost_model, lane_caps, data_freshness, role_personas, validation populated; only source_reliability + short_policy remain skeletons).
- ✓ Strategy identity in every risk decision (precheck signature includes strategy_id via OrderIntent; caller writes strategy_decision).
- ✓ Crypto exposure cap enforced (test_phase2_limits_asset_class + test_phase2_precheck_integration).
- ✓ PDT — block entries only, never exits (test_phase2_pdt).
- ✓ Hash chain verified at startup (kernel/boot.py runs verify_all_chained).
- ✓ Single-writer guard (already shipped Phase 1; boot path doesn't break it).
- ✓ Asset-class concentration check (replaces v2 factor-crowding test).
- ✓ Validation lock cooldown (policy_loader.honor_cooldown asymmetric).
- ✓ Autonomous demotion (lane_caps.demote_on_breach).

## Deferred to Phase 3+

- Live broker API error-rate detector wiring (needs hardened Alpaca adapter).
- Live data freshness watermark population (needs L1 ingest writers).
- Wall-clock skew detector wiring (needs daemon clock-tick in Phase 5).
- Stop-coverage auto-flatten (needs execution sequencer).
- Trailing-DD runtime feed (needs 60-day equity series from position_snapshot).
