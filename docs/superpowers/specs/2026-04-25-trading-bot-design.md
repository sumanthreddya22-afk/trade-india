# Trading Bot — Design Spec

**Date:** 2026-04-25
**Owner:** bharath8887@gmail.com
**Account:** Alpaca Paper Trading ($100,000 simulated)
**Mode:** Semi-autonomous, Claude-as-brain, regime-adaptive

---

## 1. Goals

**Primary:** Capital preservation. Do not lose money. Drawdown is the enemy.
**Secondary:** Beat S&P 500 returns over a 12-month rolling window with a higher Sharpe ratio.
**Tertiary:** Continuously evolve the strategy as the bot accumulates real performance data.

### Success Criteria
- Max drawdown < 15% at all times (circuit-breaker enforces)
- 12-month Sharpe ratio > 1.0
- Annualized return > S&P 500 benchmark (currently ~10-15%)
- Zero unauthorized trades, zero risk-rule violations
- 30-day paper trading with positive P&L before any real-money discussion

---

## 2. Architecture Overview

The system is built as **Claude-as-brain**: a set of MCP servers feed live market data into Claude, scheduled routines drive Claude to read state, decide trades, and execute via the Alpaca API. The strategy itself lives in markdown documents that Claude rewrites over time as it learns.

```
┌─────────────────────────────────────────────────────────┐
│  DATA MCP SERVERS (always streaming)                    │
│  alpaca-mcp │ trading-intelligence-mcp                  │
│  (future: quiverquant-mcp, unusual-whales-mcp)          │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  STRATEGY STORE (versioned, evolving)                   │
│  strategy/rules.md │ regime.json │ positions.json       │
│  strategy/performance.md │ watchlist.md                 │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  CLAUDE SCHEDULED ROUTINES                              │
│  Reads state → applies rules → decides → executes       │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  RISK MANAGER (gates every trade, no exceptions)        │
│  Position sizing │ Circuit-breaker │ Stop-loss          │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  EXECUTION (Alpaca API)                                 │
│  Stocks │ Crypto │ Options                              │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│  REPORTING (Email to bharath8887@gmail.com)             │
│  Daily P&L │ Circuit-breaker alerts │ Weekly review     │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Components

### 3.1 `alpaca-mcp` (official server, installed)
- 108 trading functions: stocks, options, crypto, market data, account management
- Used for: live prices, news, position data, order placement

### 3.2 `trading-intelligence-mcp` (custom — we build this)
**Purpose:** Aggregate all free data sources into a single Claude-facing interface.

**Free data feeds:**
| Feed | Source | Refresh | Purpose |
|---|---|---|---|
| Market data | Alpaca + yfinance | Real-time | Prices, OHLCV, technicals |
| News | Alpaca News API | Real-time | Headlines per symbol |
| Stock screener | Finviz (Python lib) | 1× daily | Build watchlist |
| Crypto | CoinGecko API | 5min | 24/7 crypto prices, sentiment |
| WSB sentiment | Reddit API | 1hr | Retail sentiment trending |
| Fear & Greed | alternative.me API | 1× daily | Market-wide sentiment score |
| Economic | FRED API | 1× daily | VIX, interest rates, yields |
| Technicals | Alpha Vantage | On-demand | RSI, MACD, Bollinger Bands |

**Tools exposed to Claude:**
- `get_market_regime()` → returns `{regime, vix, trend, volatility, confidence}`
- `get_watchlist()` → current symbols to evaluate
- `get_signals(symbol)` → all signals for symbol (momentum, mean-reversion, sentiment)
- `get_sentiment_score(symbol)` → composite sentiment (-1.0 to +1.0)
- `get_news(symbol, limit)` → recent headlines
- `get_crypto_signals()` → BTC, ETH momentum + Fear & Greed
- `update_strategy_rules(content)` → Claude rewrites `strategy/rules.md`
- `log_trade(trade_dict)` → appends to performance log
- `get_performance_summary(period)` → P&L, Sharpe, drawdown vs SPY
- `get_open_positions()` → current portfolio state
- `check_risk_limits()` → returns `{ok: bool, breached: [...]}`

**Plug-and-play extensions (when paid subscriptions activate):**
- `quiverquant-mcp` → adds `get_congress_trades()`, `get_insider_buys()`, `get_govt_contracts()`
- `unusual-whales-mcp` → adds `get_options_flow()`, `get_dark_pool_prints()`, `get_unusual_activity()`

### 3.3 Strategy Store (`/Users/bharathkandala/Trading/strategy/`)
Living documents Claude reads and rewrites:

- **`rules.md`** — Current strategy rules. Claude can rewrite weekly based on performance.
- **`regime.json`** — Current market regime + when last updated.
- **`positions.json`** — Open positions, entry prices, stop-loss levels.
- **`performance.md`** — Trade journal: every trade, P&L, what worked, what didn't.
- **`watchlist.md`** — Tradeable universe today (refreshed daily).
- **`config.yaml`** — Risk parameters, circuit-breaker thresholds, allocation limits.

### 3.4 Scheduled Routines (Claude cloud, always-on)

| Routine | Schedule | Purpose |
|---|---|---|
| `morning_brief` | 8:30 ET, weekdays | Pre-market regime detection, refresh watchlist, set day's plan |
| `intraday_scan` | Every 15min, 9:30-16:00 ET weekdays | Read MCP signals, decide entries/exits |
| `crypto_scan` | Every hour, 24/7 | Crypto-specific signal evaluation |
| `eod_review` | 16:30 ET, weekdays | Daily P&L summary email + position review |
| `weekly_evolve` | Saturday 10:00 ET | Performance review, rewrite `rules.md`, parameter tuning |
| `risk_monitor` | Every 5min during market hours | Watch for circuit-breaker conditions |

### 3.5 Risk Manager (gates EVERY trade)

**Hard rules — cannot be bypassed:**

1. **Daily loss limit:** -2% of account → halt all trading until next session
2. **Weekly loss limit:** -5% of account → halt all trading until manual review
3. **Per-trade risk:** Max 1% of account at risk per single trade (stop-loss enforces)
4. **Position size limit:** No single position > 10% of account
5. **Asset class caps:**
   - Stocks: max 70%
   - Crypto: max 30%
   - Options: max 20%, no naked options ever
   - Cash floor: 10% minimum
6. **Concentration limit:** Max 5% in any one symbol
7. **Options rules:** Only covered calls (own underlying) and protective puts. Never naked, never far OTM lottery tickets.
8. **Stop-loss required:** Every position has a stop-loss order placed within 60 seconds of entry. No exceptions.

**Dynamic allocation by regime:**

| Regime | Stocks | Crypto | Options | Cash |
|---|---|---|---|---|
| Trending Up (calm) | 60% | 25% | 15% | 0% |
| Trending Down | 30% | 15% | 10% | 45% |
| Sideways | 40% | 20% | 20% | 20% |
| Risk-Off (VIX>30) | 10% | 5% | 0% | 85% |

### 3.6 Email Reporting

**Daily 4:30pm ET email:**
- P&L for the day (absolute + %)
- vs S&P 500 daily move
- All trades executed (entry, exit, P&L)
- Open positions with unrealized P&L
- Current regime + watchlist
- Notable news/signals that drove decisions

**Instant alert emails:**
- Circuit-breaker triggered (must include reason + state)
- Stop-loss failure (rare — manual intervention needed)
- API authentication failure
- Position limit breach attempt

**Weekly Sunday email:**
- 7-day performance summary
- vs S&P 500 weekly comparison
- Strategy rules changes made
- Top 3 wins, top 3 losses, lessons learned

---

## 4. Strategy Framework

### 4.1 Regime Detection
Read every 15 minutes during market hours:

```
VIX < 20 AND SPY 50DMA > 200DMA AND 10d_vol < 1.5%  → Trending Up
VIX < 25 AND SPY 50DMA < 200DMA                      → Trending Down
VIX 20-30 AND |trend| weak                           → Sideways
VIX > 30                                             → Risk-Off
```

### 4.2 Strategy Modules

**Momentum Strategy** (active in Trending Up regime)
- Universe: Top 20 momentum stocks from Finviz screener + SPY, QQQ, BTC, ETH
- Signal: 12-day RSI > 60 AND MACD bullish crossover AND price > 20-day EMA
- Entry: Limit order at current price - 0.2%
- Exit: Trailing stop 5% below recent high OR signal reversal
- Position size: Kelly Criterion capped at 5% of account

**Mean Reversion** (active in Sideways regime)
- Universe: SPY, QQQ, large-cap tech (low-noise names only)
- Signal: RSI < 30 AND price below lower Bollinger Band AND no negative news
- Entry: Limit order at current price
- Exit: Price reverts to 20-day SMA OR 3 trading days max hold
- Position size: Fixed 2% of account per trade

**Sentiment Strategy** (always active as filter)
- Reads Alpaca News + Reddit WSB + Fear & Greed
- Phase 1: Keyword scoring (positive/negative word lists)
- Phase 2 (evolution): FinBERT transformer model for headline sentiment
- Output: Score -1.0 to +1.0 per symbol
- Used to **veto** other strategies' signals when sentiment strongly contradicts

**Options Strategy** (income + hedging only)
- **Covered calls:** Sell calls 30-45 days out, 0.20-0.30 delta, on existing stock positions
- **Protective puts:** Buy puts 60-90 days out, 0.20 delta, when regime turns Risk-Off
- **Never:** naked calls, naked puts, far-OTM lottery tickets

### 4.3 Strategy Evolution (Approach 2 → 3)

**Phase 1 (weeks 1-4):** Hard-coded rules in `rules.md`. Claude executes them faithfully.

**Phase 2 (weeks 4-12):** Claude analyzes performance weekly and tunes parameters in `rules.md`. Sentiment scoring upgraded from keywords to FinBERT.

**Phase 3 (weeks 12+):** Claude proposes new rules based on observed patterns. Each rule change is A/B tested for 2 weeks before adoption. Reinforcement learning for position sizing.

**Phase 4 (months 6+):** Full ML signal generation. Add paid data sources (QuiverQuant, Unusual Whales) once ROI justifies the spend.

---

## 5. Tech Stack

- **Python 3.11+**
- **alpaca-py** (official Alpaca SDK)
- **MCP SDK** (Python) for custom server
- **pandas, numpy, ta-lib** (technical indicators)
- **yfinance, finvizfinance, pycoingecko, praw** (data sources)
- **transformers + FinBERT** (sentiment, Phase 2)
- **APScheduler** (local scheduling for backtests)
- **SQLite** (trade journal, performance log)
- **smtplib** with Gmail App Password (email)
- **Backtrader** (backtesting framework)
- **pydantic** (config validation, type safety)

---

## 6. Project Structure

```
/Users/bharathkandala/Trading/
├── docs/
│   └── superpowers/specs/2026-04-25-trading-bot-design.md
├── trading_intelligence_mcp/
│   ├── server.py
│   ├── feeds/
│   │   ├── market_data.py
│   │   ├── sentiment.py
│   │   ├── screener.py
│   │   ├── crypto.py
│   │   └── economic.py
│   ├── state/
│   └── tests/
├── strategy/
│   ├── rules.md
│   ├── regime.json
│   ├── positions.json
│   ├── performance.md
│   ├── watchlist.md
│   └── config.yaml
├── execution/
│   ├── alpaca_client.py
│   ├── risk_manager.py
│   ├── order_manager.py
│   └── tests/
├── reporting/
│   ├── email_sender.py
│   ├── daily_report.py
│   ├── weekly_report.py
│   └── templates/
├── backtesting/
│   ├── runner.py
│   ├── strategies/
│   └── results/
├── routines/
│   ├── morning_brief.md
│   ├── intraday_scan.md
│   ├── crypto_scan.md
│   ├── eod_review.md
│   ├── weekly_evolve.md
│   └── risk_monitor.md
├── data/
│   ├── trade_journal.db
│   └── historical/
├── .env  (Alpaca keys, Gmail App Password)
├── pyproject.toml
└── README.md
```

---

## 7. Foolproofing — Safeguards

This is paper trading first, but the safeguards must be production-grade so we can graduate to real money confidently.

### 7.1 Pre-trade Checks (every order, no exceptions)
1. Risk Manager validates the trade against ALL hard rules before sending to Alpaca
2. If any rule fails → trade is rejected, logged, and emailed to user if persistent
3. Stop-loss order is placed atomically with the entry order

### 7.2 Circuit Breakers
- **Daily P&L < -2%** → halt all new trades, close intraday positions only if losses worsen, send instant email
- **Weekly P&L < -5%** → halt completely until manual review email response
- **3 consecutive losing days** → reduce position sizes by 50% automatically
- **API failure rate > 5%** in 1 hour → halt and alert

### 7.3 State Integrity
- All state changes go through transactional writes (rename-after-write pattern)
- Position state reconciled with Alpaca account every 15 minutes — discrepancies trigger alert
- Trade journal is append-only, never edited
- Daily snapshot of full state archived

### 7.4 Backtesting Gate
- Every new rule MUST be backtested against minimum 2 years historical data before activation
- Must show: Sharpe > 1.0, Max DD < 15%, positive return in at least 3 of last 4 quarters
- Out-of-sample test (last 6 months held out) required

### 7.5 Paper-First Doctrine
- 30 calendar days of paper trading with positive P&L before ANY discussion of real money
- All evolution (rule changes, new strategies) tested in paper for 2 weeks minimum
- Real money never moves without explicit user authorization

### 7.6 Failure Modes & Recovery
| Failure | Detection | Recovery |
|---|---|---|
| Alpaca API down | HTTP errors / timeout | Retry with backoff, halt new trades after 3 fails |
| Bad data feed | Stale timestamps / nulls | Skip strategy that depends on it, alert user |
| Stop-loss not placed | Post-entry verification check | Cancel parent order, alert immediately |
| Position desync | 15-min reconciliation | Trust Alpaca state, alert user |
| Strategy rule corrupted | Schema validation on read | Roll back to last known-good `rules.md` |
| Email send failure | smtplib exception | Retry 3x, log to local file, dump to stderr |
| Claude routine misfire | Cron heartbeat missing | Next routine detects gap, re-checks state |

---

## 8. Performance Benchmarking

**Comparison baseline:** SPY (S&P 500 ETF) buy-and-hold returns over the same period.

**Tracked metrics (updated daily):**
- Cumulative return % (bot vs SPY)
- Annualized return (rolling 90-day)
- Sharpe ratio (12-month rolling, target > 1.0)
- Sortino ratio (downside deviation focus)
- Max drawdown (target < 15%)
- Win rate (target > 50%)
- Profit factor (target > 1.5)
- Average holding period
- vs SPY beta (lower = more uncorrelated alpha)

**Expected returns (modeled, NOT guaranteed):**

| Scenario | Bot Annual Return | SPY Annual Return | Bot Max DD | SPY Max DD |
|---|---|---|---|---|
| Conservative | 12-15% | 10-12% | < 10% | 15-20% |
| Base case | 15-20% | 10-12% | < 12% | 15-20% |
| Bull case | 20-30% | 10-12% | < 15% | 15-20% |

> **Important:** These are MODELED expectations based on backtest research. Actual returns will vary. Capital preservation > beating SPY.

---

## 9. Phased Build & Rollout

### Phase 0: Foundation (Day 1)
- Install Alpaca MCP server
- Set up project structure, virtualenv, dependencies
- Verify Alpaca API connectivity (paper account)
- Email sender (Gmail App Password) tested end-to-end

### Phase 1: Core MCP + Risk Manager (Days 2-4)
- Build `trading-intelligence-mcp` with market_data, screener, crypto feeds
- Build risk_manager.py with all hard rules
- Build alpaca_client.py wrapper with stop-loss enforcement
- Unit tests for risk manager (foolproof gate)

### Phase 2: Strategy Store + First Routines (Days 5-7)
- Author initial `rules.md`, `config.yaml`
- Build `morning_brief` and `eod_review` routines
- Wire email reporting (daily summary)
- First trades begin (paper)

### Phase 3: Full Routines + Sentiment (Days 8-12)
- Add `intraday_scan`, `crypto_scan`, `risk_monitor` routines
- Add sentiment feeds (Reddit, Fear & Greed, news)
- Phase 1 keyword sentiment scoring active

### Phase 4: Backtesting Framework (Days 13-16)
- Backtrader integration
- Backtest current `rules.md` against 2 years historical data
- Validate Sharpe > 1.0, DD < 15% before continuing live paper trading

### Phase 5: Evolution Loop (Day 17+)
- Weekly evolve routine active
- Performance log feeds rule tuning
- 30-day paper trading milestone → review for graduation

### Phase 6+: ML Upgrades & Paid Data (when justified)
- FinBERT sentiment
- QuiverQuant + Unusual Whales MCP integration
- Reinforcement learning for sizing

---

## 10. Open Items / User Action Required

- **Gmail App Password** — needed to send daily emails. User generates at myaccount.google.com → Security → App Passwords.
- **API subscriptions (future):** QuiverQuant Hobbyist ($10/mo) and Unusual Whales (free tier) once bot is profitable in paper trading for 30+ days.
- **Real money decision:** Not now. Revisit only after 30 days positive paper P&L + user approval.

---

## 11. Out of Scope (Explicitly NOT Building)

- HFT / microsecond-latency strategies (impossible without colocated infra)
- Forex trading (Alpaca doesn't support it)
- Naked options selling (too risky for capital preservation goal)
- Penny stocks / OTC (illiquid, manipulation-prone)
- Leveraged ETFs (decay too risky for hold periods we use)
- Tax optimization (paper trading, not relevant yet)
- Multi-user / multi-account support (single user only)
