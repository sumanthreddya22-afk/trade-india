# Strategy Rules — Phase 1

**Last updated:** 2026-04-25
**Phase:** 1 (rule-based momentum)

## Active Strategies

### 1. Momentum Entry (long only)

**When to enter a BUY:**
- 14-day RSI is between 55 and 70 (rising but not overbought)
- MACD line is above the signal line (bullish momentum)
- Current price is above the 20-day EMA
- 5-day return is positive

**When to skip:**
- RSI > 70 (already overbought, late to the party)
- RSI < 55 (no momentum, would be mean-reversion territory)
- Price below 20-day EMA (downtrend)

**Position sizing:**
- Risk 0.5% of equity per trade (target half of the 1% per-trade cap to leave room for stop slippage)
- Stop-loss at the 20-day EMA OR 5% below entry, whichever is closer
- Position size = (risk dollars) / (entry - stop)

**Exit (managed in Plan 3):**
- Hard stop-loss at the calculated price (set atomically with entry)
- Trail stop to entry + 2% once unrealized gain ≥ 5%

## Inactive Strategies (added in later plans)

- Mean Reversion (Plan 3)
- Sentiment overlay (Plan 3 — will use Alpaca news + GDELT/SEC EDGAR/FRED feeds via MCP)
- Options (Plan 4 — covered calls, protective puts only)

## Performance Targets (12-month rolling)

- Sharpe ratio > 1.0
- Max drawdown < 15%
- Annualized return > S&P 500
- Win rate > 50%
- Profit factor > 1.5

## Evolution Log

(Claude updates this section when rules change. Each entry lists date + reason + rule diff.)

- 2026-04-25 — initial rules authored.

### 2026-04-25 — performance review
- No strategy has accumulated enough closed trades yet.
- No rule changes proposed.
