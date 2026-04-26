# Plan 5d — Dynamic Position Sizing

**Status:** Design (paused; re-enters queue after backtest harness lands)
**Date:** 2026-04-26
**Parent plan:** [2026-04-25-plan-5-adaptive-intelligence.md](2026-04-25-plan-5-adaptive-intelligence.md)
**Sequence note:** Originally numbered 5b. Re-sequenced to 5d per [2026-04-26-revised-plan-sequence.md](2026-04-26-revised-plan-sequence.md) — backtest harness (new 5b) supplies the empirical data needed to set the tuning knobs (`conviction_floor`, `target_atr_pct`, `mult_ceiling`) instead of guessing them.

## Goal

Replace flat position sizing with a multiplier-based sizing layer driven by signal conviction, instrument volatility, and portfolio correlation. The risk manager remains an unmodified pass/fail safety gate; all dynamism lives in a new sizing layer between strategy evaluation and risk checking.

## Non-Goals

- No changes to `RiskManager` gate logic. The same caps (`per_trade_risk_pct`, `max_position_pct`, `max_symbol_concentration_pct`, `regime_allocations`) remain inviolable safety rails.
- No Kelly-criterion sizing or win-rate-based scaling. Conviction is treated as a pre-computed signal strength input, not derived from historical PnL.
- No pairwise return-correlation computation. Correlation is approximated via sector-tag overlap.
- No changes to `strategy.evaluate` — it continues to produce a `base_qty` from the flat per-trade risk budget.

## Architecture

```
opportunities.md
  → load_ranked_watchlist()           # extended: carries conviction + sector_tags
    → for each entry:
        strategy.evaluate(...)        → base_qty            (unchanged)
        position_sizer.size(entry, base_qty, positions, bars, sector_map)
            ├─ conviction_mult        (from entry.conviction)
            ├─ volatility_mult        (from bars + per-class ATR target)
            ├─ correlation_penalty    (from positions ∩ sector_tags)
            └─ final_qty (or 0 → skipped_low_conviction)
        risk_manager.check(...)       (unchanged — same gates)
        place_order(...)
```

The new module `src/trading_bot/position_sizer.py` is the only place dynamic-risk math lives. The orchestrator gains exactly one call site between `strategy.evaluate` and `risk.check`.

## Components

### 1. `state.WatchlistEntry` — extended

Add two optional fields. YAML loader (legacy) leaves defaults; `load_ranked_watchlist` populates real values from `opportunities.md`.

```python
@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    asset_class: str
    notes: str
    conviction: float | None = None          # NEW — 0..1, None for legacy yaml
    sector_tags: tuple[str, ...] = ()        # NEW — empty for legacy yaml
```

### 2. `screener.load_ranked_watchlist` — wider channel

Currently parses `opportunities.md` and discards conviction + sector tags. Updated parser populates both fields on `WatchlistEntry`. The markdown round-trip must preserve them; if the current `render_opportunities_snapshot` does not emit sector tags, it gains a `Sectors:` line per candidate.

### 3. `config.SizingConfig` — new block

```python
class SizingConfig(BaseModel):
    conviction_floor: float = Field(0.3, ge=0, le=1)
    atr_lookback_days: int = Field(14, ge=5, le=60)
    target_atr_pct: dict[str, float] = {
        "stock":  0.02,
        "crypto": 0.05,
        "option": 0.04,
    }
    mult_floor: float = Field(0.25, ge=0.1, le=1.0)
    mult_ceiling: float = Field(2.0, ge=1.0, le=5.0)
    correlation_per_match: float = Field(0.3, ge=0, le=1)

class AppConfig(BaseModel):
    # ... existing ...
    sizing: SizingConfig = SizingConfig()
```

### 4. `position_sizer.size` — the math

```python
def size(
    entry: WatchlistEntry,
    base_qty: int,
    *,
    bars: pd.DataFrame,
    positions: list[Position],
    sector_map: dict[str, tuple[str, ...]],
    cfg: SizingConfig,
) -> SizingResult: ...

@dataclass(frozen=True)
class SizingResult:
    final_qty: int                       # 0 means skip
    conviction_mult: float
    volatility_mult: float
    correlation_penalty: float
    combined_mult: float                 # post-clamp
    skip_reason: str | None              # "low_conviction" | "rounded_to_zero" | None
```

**Step 1 — Conviction**

```python
if entry.conviction is None or entry.conviction < cfg.conviction_floor:
    return SizingResult(final_qty=0, ..., skip_reason="low_conviction")
conviction_mult = clamp(0.5 + entry.conviction, 0.5, 1.5)
```

**Step 2 — Volatility**

```python
atr_value = atr(bars, n=cfg.atr_lookback_days)        # from indicators.py
last_close = float(bars["close"].iloc[-1])
atr_pct = atr_value / last_close if last_close > 0 else 0.0
target = cfg.target_atr_pct.get(entry.asset_class, 0.02)
volatility_mult = clamp(target / atr_pct, 0.5, 1.5) if atr_pct > 0 else 1.0
```

**Step 3 — Correlation penalty**

```python
overlap = sum(
    1 for p in positions
    if any(t in entry.sector_tags for t in sector_map.get(p.symbol, ()))
)
correlation_penalty = 1.0 / (1.0 + cfg.correlation_per_match * overlap)
```

**Step 4 — Combine, clamp, round**

```python
combined = conviction_mult * volatility_mult * correlation_penalty
combined = clamp(combined, cfg.mult_floor, cfg.mult_ceiling)
final_qty = int(math.floor(base_qty * combined))
if final_qty <= 0:
    return SizingResult(final_qty=0, ..., skip_reason="rounded_to_zero")
return SizingResult(final_qty=final_qty, ...)
```

### 5. `indicators.atr` — reuse or add

If `indicators.py` already exposes an ATR helper, reuse it. Otherwise add a small `atr(bars, n) -> float` using the standard true-range formula. ATR lives in indicators (centralized), not in the sizer.

### 6. `orchestrator` — single insertion

Between `strategy.evaluate` and `risk.check`:

```python
sizing = self._sizer.size(
    entry, base_qty=sig.qty,
    bars=bars, positions=positions, sector_map=self._sector_map,
    cfg=self._cfg.sizing,
)
if sizing.final_qty == 0:
    decisions.append(Decision(
        symbol=symbol, action="skipped_low_conviction",
        reason=sizing.skip_reason,
        base_qty=sig.qty, final_qty=0,
        conviction_mult=sizing.conviction_mult,
        volatility_mult=sizing.volatility_mult,
        correlation_penalty=sizing.correlation_penalty,
    ))
    continue
order = OrderRequest(..., qty=sizing.final_qty, ...)
```

`self._sector_map` is built once per scan from the same `build_universe()` snapshot the screener uses; held symbols missing from the map contribute zero overlap.

### 7. `Decision` and `TradeRecord` — observability

`Decision` gains optional fields: `base_qty`, `final_qty`, `conviction_mult`, `volatility_mult`, `correlation_penalty` (populated for `placed_order` and `skipped_low_conviction`).

`TradeRecord` gains `sizing_breakdown: str` — a one-line summary like `"conv=1.32×vol=0.95×corr=0.77→0.97"` for post-hoc analysis.

A new action code is introduced: `skipped_low_conviction`.

## Data Flow

1. **Screener** writes `opportunities.md` with conviction + sectors per candidate.
2. **Orchestrator** loads ranked watchlist; for each scan, builds `sector_map` from the latest universe snapshot.
3. **Per candidate:** strategy → base_qty → sizer → final_qty → risk gate → order.
4. **Decisions** carry the full multiplier breakdown for any sized trade; **TradeRecord** persists it.

## Error Handling

| Condition | Behavior |
|---|---|
| `entry.conviction is None` | skip with `skipped_low_conviction` |
| `entry.conviction < floor` | skip with `skipped_low_conviction` |
| `atr_pct == 0` (flat bars) | `volatility_mult = 1.0` (no div-by-zero) |
| `last_close == 0` | `volatility_mult = 1.0` |
| held symbol not in `sector_map` | contributes 0 overlap |
| `final_qty` rounds to 0 | skip with `skipped_low_conviction`, reason="rounded_to_zero" |
| unknown `asset_class` in `target_atr_pct` | falls back to stock target (0.02) |

The risk manager remains the last line of defense — even an aggressive multiplier cannot push a trade past `per_trade_risk_pct` or any other gate.

## Testing

### `tests/test_position_sizer.py` — 15 unit tests, TDD

| # | Test | Asserts |
|---|---|---|
| 1 | conviction below floor | `final_qty == 0`, `skip_reason == "low_conviction"` |
| 2 | conviction = None | `final_qty == 0` |
| 3 | conviction at floor (0.3) | conviction_mult = 0.8, sizes through |
| 4 | conviction = 1.0 | conviction_mult = 1.5 |
| 5 | ATR exactly at class target | volatility_mult = 1.0 |
| 6 | ATR = 2× target | volatility_mult clamped to 0.5 |
| 7 | ATR = 0.5× target | volatility_mult clamped to 1.5 |
| 8 | crypto with 5% ATR | volatility_mult = 1.0 (per-class) |
| 9 | zero same-sector positions | correlation_penalty = 1.0 |
| 10 | 3 same-sector positions | penalty ≈ 0.526 |
| 11 | combined → ceiling clamp | high-conv + low-vol + uncorrelated |
| 12 | combined → floor clamp | low-conv + high-vol + correlated |
| 13 | base_qty=1, multiplier=0.4 → final=0 | rounds to zero, skipped |
| 14 | held symbol missing from sector_map | overlap = 0 |
| 15 | flat bars (ATR=0) | volatility_mult = 1.0, no exception |

### Orchestrator integration tests (extend `tests/test_orchestrator.py`)

- Ranked entry with `conviction=None` → `skipped_low_conviction` decision.
- Ranked entry with valid conviction → `placed_order` decision carries multiplier breakdown.
- `final_qty` is what reaches `risk.check` (verify the gate sees the post-multiplier qty, not `base_qty`).
- `TradeRecord.sizing_breakdown` populated on placed orders.

### Config test

- `SizingConfig` defaults validate.
- Bad values (e.g. `conviction_floor=1.5`) raise.

## Migration

- Legacy `watchlist.yaml` path: `WatchlistEntry.conviction` defaults to `None`, which under this design **skips every legacy-loaded entry**. This is intended — Plan 5a established `opportunities.md` as the source of truth; the YAML path is dead code from the bot's perspective. If kept for tests, those tests must construct `WatchlistEntry` with explicit conviction.

- No data migration. No persisted state changes shape (only `TradeRecord` gains an optional field, written forward).

## Out of Scope (Plan 5c+)

- Tiered cadence / event bus.
- VIP tweet sentiment as a sizing input.
- Pairwise return-correlation (vs. the sector-overlap approximation used here).
- Conviction calibration from realized PnL.
