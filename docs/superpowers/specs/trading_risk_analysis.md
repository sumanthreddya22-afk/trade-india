# Trading Bot — Failure Risk Analysis
*From the perspective of a seasoned trader. Read-only review. No code was changed.*

**Date:** 2026-04-26  
**Account:** Alpaca Paper Trading — $15,000 simulated  
**Horizon:** Next 2–3 months

---

> **Bottom line up front:** The architecture is genuinely solid — good guard-rails, clean separation of concerns, paper-only enforcement. But the bot has **zero closed trades** (the portfolio snapshot shows `"positions": {}` and `rules.md` explicitly says *"No strategy has accumulated enough closed trades yet"*). It is trading live signals with **unvalidated parameters** on signals that have **never been backtested**. That's the single deepest risk. Everything below flows from there.

---

## Risk 1 — Strategy Parameters Are Empirically Unvalidated
**Severity: 🔴 CRITICAL**

### What the code actually does
- `MomentumStrategy`: enters on RSI 55–70, MACD bullish, price > EMA20, 5d return > 0.
- `MeanReversionStrategy`: enters on RSI 25–35, price near/below EMA20.
- The RSI windows, stop percentages (5% momentum / 4% mean-reversion), and per-trade risk budget (0.5%) were authored on **2026-04-25** with zero empirical backing — `rules.md` confirms "initial rules authored" and "no strategy has accumulated enough closed trades."

### Why it will fail
These parameters work in one regime and destroy capital in another.

**Momentum RSI band (55–70):** In a choppy, news-driven market (very likely in a tariff/geopolitical 2026 environment), RSI bounces wildly between 45 and 65 without producing directional moves. You'll enter, your 5% EMA-stop will be too loose to protect and too tight to breathe, and you'll get chopped up.

**Mean-reversion 4% stop:** Oversold stocks in a true downtrend regularly gap through a 4% stop overnight. You won't have time to react — the stop will fill 6–8% below entry in a genuine market dislocation.

**No take-profit logic in the strategy itself:** The bot places 2:1 bracket orders (entry+stop+take-profit), but the take-profit is set mechanically at `entry + 2×risk` with no consideration of nearest resistance, volume profile, or regime target. In a sideways regime, you will frequently stop out before hitting 2:1.

### Mitigation
- **Do NOT proceed to Plan 5 feature work until you have 30+ closed trades** per strategy in the current regime with a Sharpe > 1.0. Plan 5 adds complexity on top of an unverified core.
- Run the `bot backtest` harness (already spec'd in `2026-04-25-plan-5-adaptive-intelligence.md` §13) against 2 years of historical data. This is mandatory before calling parameters safe.
- Add ATR-based stops (Plan 5b already designs this — good) rather than flat % stops. A 4% stop on a name with 30% annualized vol is nearly noise.

---

## Risk 2 — No Meaningful Exit Strategy for Winning Trades
**Severity: 🔴 CRITICAL**

### What the code actually does
The only exit mechanism is the **bracket order** placed at entry:
- Hard stop at EMA20 or 5% below entry (whichever is **higher**, which in `strategy.py` is computed as `max(ema_stop, pct_stop)` — this is actually correct geometry but the magnitudes are fixed).
- Take-profit at `entry + 2 × risk`. With a 5% stop, the take-profit is set 10% above entry.

There is **no trailing stop implementation** despite the design spec mentioning "trail stop to entry +2% once unrealized gain ≥5%." The `rules.md` says "Exit (managed in Plan 3)" — meaning it was never built.

### Why it will fail
10% take-profit targets on large-cap momentum names (SPY, AAPL, MSFT, QQQ) in sub-trending regimes will **almost never hit** within a reasonable hold period. The position will oscillate below the target, your capital will be tied up, and eventually the stop will clip you at breakeven or small loss. This is a profit-factor crusher — you will have lots of breakeven trades and occasional losses, but rare wins.

On crypto (BTC/USD, ETH/USD), a 10% target is reasonable but the 5% hard stop is inadequate given crypto's daily ATR regularly exceeds 3–5%. You will get stopped out intraday on noise.

### Mitigation
- Implement the trailing stop logic that was spec'd. Even a simple "move stop to breakeven when +3%, trail at 50% of unrealized P&L above breakeven" materially improves profit factor.
- For crypto specifically: the stop should be calibrated to ATR, not a fixed 5% (Plan 5b partially addresses this with volatility_mult — prioritize that for crypto).
- Consider time-based exits: if a position hasn't moved 5% in 3 days, exit. Dead money is a cost.

---

## Risk 3 — The Regime Detector Will Misclassify in Fast-Moving Markets
**Severity: 🟠 HIGH**

### What the code actually does
`regime.py`'s `detect_regime_from_bars()` classifies based on:
- SPY close vs EMA50 vs EMA200 (long-lag indicators).
- 20-day realized volatility vs 30% annualized threshold for risk_off.
- 20-day max drawdown vs -10% for risk_off.

### Why it will fail
**EMA50 and EMA200 are among the slowest possible regime signals.** In April 2026, we've already experienced tariff shock / macro regime flips that cause the market to drop 8% in a week, then recover 5%, then drop again. During this volatility:

1. EMA50 > EMA200 will still be true (golden-cross territory from prior bull run) even as the market is functionally in a distribution/markdown phase. The bot will classify as `trending_up` and put **60% in stocks + 25% in crypto** — an extremely aggressive allocation for what is actually a risk-off environment.

2. The 20-day vol threshold of 30% annualized is too high. The S&P 500's annualized vol in the March–April 2025 correction hit ~25–28%. The bot would classify that period as *non-risk-off* and keep trading aggressively, when any experienced trader would be sitting on cash.

3. The `confidence: "medium"` sideways case is the most dangerous. The bot still trades in sideways — it switches to MeanReversionStrategy — but mean reversion in a volatile sideways regime with macro uncertainty produces frequent false signals.

### Evidence from config
```yaml
regime_allocations:
  trending_up: {stocks: 60.0, crypto: 25.0, options: 15.0, cash: 0.0}
```
**Zero cash floor in trending_up.** If the regime detector fires `trending_up` incorrectly, the bot will be fully invested with no defensive buffer.

### Mitigation
- Lower the risk_off vol threshold from 30% to **20–22%** annualized (more realistic for when professionals start hedging).
- Add VIX as a primary input (not just SPY volatility proxy). The FRED feed for `VIXCLS` is already implemented in `intelligence.py` — wire it into regime detection directly. A VIX > 22 should immediately trigger at least `sideways` regime; VIX > 28 triggers `risk_off`.
- Add a **mandatory 10% cash floor** even in trending_up. A fully-invested algo with a $15k account that hits a 2% daily circuit-breaker loses $300 and halts — it has no room to maneuver.

---

## Risk 4 — Crypto Stop-Loss Is Non-Atomic and Will Fail in Flash Crashes
**Severity: 🟠 HIGH**

### What the code actually does
`alpaca_client.py` line 210 — `_place_crypto_with_stop()`:
```python
entry_req = MarketOrderRequest(...)   # fills instantly
stop_req = StopOrderRequest(...)      # placed in a SECOND API call
```
These are **two separate calls with no atomicity**. The comment in the code even acknowledges: *"Crypto market orders fill near-instantly; cancellation likely won't help."*

### Why it will fail
Crypto markets have flash crashes measured in **seconds** (BTC/ETH are 24/7, liquidity thins overnight). If:
- The market order fills.
- Alpaca's API experiences any latency (a 500ms delay is common under load).
- BTC drops 3% in those 500ms before the stop is placed.

You're now in a losing position with **no stop**. In a real flash crash (like the $LUNA-style cascades we've seen historically), this is not a scenario — it's a guarantee.

### Mitigation  
- For crypto, use Alpaca's **fractional market order + trailing stop** in a single bracket if their API supports it, or switch to a **stop-limit below entry** placed as fast as possible post-fill.
- Add a **post-fill verification loop**: after placing the entry, sleep 100ms, verify position exists, verify stop exists — if stop is missing, immediately flatten the position (market sell). This is a safety net in code.
- Consider running crypto through `bot dry-run` before real execution to verify the stop architecture works.

---

## Risk 5 — The Evolution Loop Cannot Learn Because There Are No Closed Trades
**Severity: 🟠 HIGH**

### What the code actually does
`evolution.py` `propose_rule_changes()` requires **minimum 20 trades per strategy** before making any proposal (and the code has an even earlier gatekeeping check at 5). `rules.md` currently shows *"No strategy has accumulated enough closed trades yet."*

`evolution.py` only tunes `per_trade_risk_pct` and `stop_pct`. It does not tune:
- RSI thresholds
- MACD parameters
- EMA window
- Hold period / exit logic

### Why it will fail
The evolution loop is the bot's immune system. If the core parameters are wrong (Risk 1), the bot needs feedback to self-correct. But with a 5% hard stop, trades can hold for weeks before closing — especially large-cap names that don't trend strongly. **The feedback loop will be too slow.**

Even when trades close, the proposals are extremely conservative — `cur_risk * 0.5` down, `cur_risk * 1.25` up. After a bad run, the bot halves its risk, then half-again, eventually entering trivially small positions. After a good run, it only scales 25% at a time. This is a highly conservative Kelly approach that won't compound wins meaningfully.

### Mitigation
- Run the **backtest harness first** to generate synthetic closed trades across 24 months. Use these to warm up the evolution parameters before any live decisions matter.
- Add RSI threshold tuning to the evolution proposals (if momentum win rate is poor, widen the RSI window).
- Track **regime-specific performance**: a strategy can have 60% win rate in `trending_up` and 30% in `sideways`. The evolution loop needs to condition on regime, not aggregate across them.

---

## Risk 6 — The Daily/Weekly Halt Is a Trap in Volatile Markets
**Severity: 🟡 MEDIUM-HIGH**

### What the code actually does
```yaml
risk:
  daily_loss_limit_pct: 2.0
  weekly_loss_limit_pct: 5.0
```
Once triggered, `halted=True` requires a **manual reset**.

### Why it will fail
In a volatile market week (VIX 28+), a -2% day on $15,000 is **just $300**. That's 2 bad trades at 0.5% risk each going full stop-loss. In a real selloff, you can lose 2% on one position in pre-market gaps alone — the bot halts, you can't trade, and you miss the recovery.

More critically: **there is no partial-halt or position-reduction mechanism at halt trigger**. The open positions continue to hold (stops remain in Alpaca as bracket legs), but the bot can't add protective hedges, can't trim, can't do anything. Open positions continue bleeding while the bot is locked out.

`pnl_state.py` correctly calculates daily/weekly PnL from Alpaca portfolio history — but it uses `period="1W"` with `timeframe="1D"` which means it only sees **daily** resolution. If you lose 2% intraday and recover to -1.5% by EOD, the bot may not halt (it sees the daily close), but if you opened at -3% pre-market the next day, it might also miss that.

### Mitigation
- Add a **soft halt** at -1.5% daily: stops taking new entries, but doesn't lock out entirely.
- The hard halt at -2% should trigger immediate **email + cancel all pending orders** (the pending open bracket entries should be cancelled, while existing stop-loss legs on filled positions remain active).
- Wire the `portfolio-watch` monitor to run every 5 minutes during market hours so the halt decision uses intraday equity, not just daily close data.

---

## Risk 7 — Overfitting to Large-Cap Liquid Names That Everyone Else Watches
**Severity: 🟡 MEDIUM-HIGH**

### What the code actually does
The current watchlist is: **SPY, QQQ, AAPL, MSFT, AMD, BTC/USD, ETH/USD**.

The momentum signal (RSI 55–70, MACD bullish, price > EMA20) on these names means the bot is competing against:
- All retail algo traders running the same RSI/MACD combo.
- Prop trading desks with microsecond execution.
- Options market makers who front-run predictable signal clusters.

### Why it will fail
These are the **most crowded signals in the most watched names on the market**. When RSI(AAPL) crosses 55 with MACD bullish, every retail algo from Robinhood to Webull to every Python trading course fires simultaneously. The edge has been arbed to zero or negative. You'll enter at the top of the momentum surge and be holding when it reverses.

The screener in Plan 5 (universe expansion to ~3,000 names) is the right answer strategically, but Plan 5 isn't deployed yet — and the current `scan` / `intel-scan` / `full-run` commands still read from `strategy/watchlist.yaml` which has these 7 names.

### Mitigation
- The `rank` command and screener are already built — **prioritize getting `strategy/opportunities.md` generation actually running on a cron**. Even 30 candidates from stage-1/2 is dramatically better than 7 hardcoded names.
- Until the full screener is wired, manually expand the watchlist to include 2–3 mid-cap names in different sectors (e.g., XLV health sector ETF, XLE energy, a semiconductor mid-cap like AMAT or KLAC). This immediately reduces crowding.

---

## Risk 8 — No Intraday Stop Monitoring (The Bot Is Blind Between Scans)
**Severity: 🟡 MEDIUM**

### What the code actually does
The scheduled scans run at fixed intervals (`full-run`, `intel-scan`). The `portfolio-watch` command detects equity changes by comparing snapshots, but it doesn't issue exit orders — it only **emails an alert**.

The bracket stop-loss orders in Alpaca are passive — they sit as GTC orders and execute if/when price hits. This is correct. **But:**
1. The bot doesn't trail these stops.
2. The bot doesn't detect if a stop leg was **cancelled** (e.g., Alpaca maintenance, API timeout during bracket creation).
3. If a position moves +5% favorably, the bot doesn't lock in gains — it just holds until either the take-profit hits (10% away) or the static stop fires.

### Why it will fail
Bracket orders are not magically protected from:
- **OCO cancellation bugs**: Alpaca has had historical bugs where a parent bracket order's legs get detached on partial fills.
- **Crypto position without an atomic stop** (Risk 4 above).
- **Earnings gaps**: a position in AMD or AAPL through an earnings event can gap 8% overnight in either direction. Your 5% stop is already inside a typical earnings gap — it fills at a much worse price on the open.

### Mitigation
- Add **earnings-window detection** to the screener (already mentioned in Plan 5 spec as `earnings-window flags` — implement it). Never enter a position within 3 trading days of earnings for stocks.
- Add a **stop verification check** post-scan: after each placed order, query Alpaca in 30 seconds to verify bracket legs are active. If the stop leg is missing, immediately flatten via market order.
- Implement the trailing stop in code (not just in the spec). It can be as simple as: if unrealized_pl > X%, cancel and replace the stop leg to lock in partial gain.

---

## Risk 9 — The Conviction-Based Position Sizer (Plan 5b) Is Still a Design Doc
**Severity: 🟡 MEDIUM**

### What the code actually does
`plan-5b-dynamic-risk-design.md` is beautifully designed — ATR-normalized volatility sizing, conviction multiplier, sector-correlation penalty. However:
- `position_sizer.py` does **not exist** in the source tree.
- The orchestrator does **not call any dynamic sizer** — it uses `strategy.evaluate()` which computes a fixed risk-budget-based `base_qty` with no dynamic adjustment.
- `WatchlistEntry` does not have `conviction` or `sector_tags` fields yet.

### Why it will fail
Without dynamic sizing, two scenarios are equally sized:
- A high-confidence breakout in a low-volatility sector with no correlated positions: sized at 0.5% risk.
- A low-confidence mean-reversion play in a high-vol, already-crowded sector: also sized at 0.5% risk.

The inability to **size up on strong signals** means the strategy's best ideas don't drive returns. The inability to **size down on crowded, high-vol, low-conviction plays** means bad ideas take equal bites of capital.

### Mitigation
- This is a high-value, low-risk code addition (the design is thorough). **Prioritize Plan 5b implementation** next, before adding the VIP-tweet pipeline or rich email reports.
- In the interim, manually gate the `mean_reversion` strategy to only fire in `sideways` and `risk_off` regimes (it already does in `strategy_for_regime()`) and manually reduce its `per_trade_risk_pct` to 0.25% (half of momentum). This is a hacky approximation but reduces worst-case loss while 5b ships.

---

## Summary Risk Table

| # | Risk | Severity | Probability | Capital Risk |
|---|---|---|---|---|
| 1 | Unvalidated strategy parameters | 🔴 Critical | Very High | Up to full loss |
| 2 | No meaningful exit / trailing stops | 🔴 Critical | High | Sustained profit-factor < 1.0 |
| 3 | Regime detector misclassifies in volatility | 🟠 High | High | Overexposure in selloff |
| 4 | Crypto stop non-atomic (flash crash risk) | 🟠 High | Low-Medium | Sudden single-trade wipeout |
| 5 | Evolution loop too slow to self-correct | 🟠 High | High | Slow capital bleed |
| 6 | Daily halt mechanism too blunt | 🟡 Med-High | Medium | Paralysis during recovery |
| 7 | Crowded signals on 7 well-watched names | 🟡 Med-High | Very High | Structural edge erosion |
| 8 | No intraday stop monitoring / trailing | 🟡 Medium | Medium | Individual trade blowups |
| 9 | Plan 5b sizer is unbuilt | 🟡 Medium | Certain | Missed upside + equal-sized bad bets |

---

## Priority Order for Mitigation (Highest ROI)

1. **Run the backtest harness** (already spec'd). Generate 2 years of simulated closed trades. Validate parameters BEFORE trusting live signals.
2. **Expand watchlist immediately** from 7 to 25–30 names using the `rank` command that is already built and working. Stop competing on the most crowded signals.
3. **Wire VIX directly into regime detection.** It's already fetched from FRED. A VIX > 22 should mechanically shift allocation toward more cash.
4. **Implement Plan 5b position sizer.** It's fully designed. Code it next sprint and immediately improve the quality of every single position-size decision.
5. **Fix crypto stop non-atomicity.** Add the post-fill stop verification loop. Low effort, eliminates a tail-risk scenario.
6. **Implement trailing stops.** Even a simple "move to breakeven at +3%" would dramatically improve the profit factor on winning trades.
7. **Add earnings-window exclusion.** Any open-source earnings calendar (or even the existing Alpaca news feed) reveals upcoming earnings. Don't hold or enter around them.

---

## One Trader's Assessment

> The infrastructure is genuinely impressive — the risk gating is sound, the paper-only enforcement is correct, the reconciliation and journaling are thorough. The **engineering quality is not the problem.**  
>
> The problem is **operating sequence**: you're building Plan 5 (adaptive intelligence, VIP sentiment, rich emails) on top of a Plan 1 strategy that has never been validated. Every hour spent on tweet sentiment pipelines is an hour not spent answering: *"Does this RSI/MACD/EMA combo actually make money on these names?"*  
>
> **The fastest path to profitability is to pause Plan 5 feature work, backtest the existing rules for 2+ years, fix the exit logic, and accumulate 30 paper trades before building anything new.**

