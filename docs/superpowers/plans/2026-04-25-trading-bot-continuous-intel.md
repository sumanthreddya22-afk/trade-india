# Trading Bot — Continuous Intelligence (Plan 4)

**Goal:** High-frequency intelligence scanning + rich reports throughout the trading day. Replace the single 10am daily run with 4 routines that match real trading workflow.

## New routines (replace daily-full-run)

| Routine | Schedule (ET) | Cron (local ET) | What it does |
|---|---|---|---|
| `intel-scan` | Every 15min during market hours | `*/15 9-15 * * 1-5` | Pull news + signals, place trades on actionable signals, silent unless trade placed |
| `portfolio-watch` | Every 15min during market hours | `7,22,37,52 9-15 * * 1-5` (offset 7 min) | Detect fills, stop triggers, large unrealized moves; email alert on material change |
| `rich-report-mid` | 12:30 ET weekdays | `30 12 * * 1-5` | Comprehensive HTML email: regime, positions, news per holding, macro, decisions |
| `rich-report-eod` | 16:30 ET weekdays | `30 16 * * 1-5` | End-of-day rich report + reconciliation |

## Data sources we'll wire in (all free)

| Source | What we get | Method | Rate limit |
|---|---|---|---|
| Alpaca News API | Real-time financial news per symbol | `alpaca-py` SDK (already integrated) | None for paper |
| FRED | VIX, 10Y yield, fed funds, unemployment | REST API (anonymous tier) | Light |
| GDELT 2.0 | Global news events, sentiment scoring | REST `doc/v2/doc` API | None |
| SEC EDGAR | Form 4 insider trades (RSS feed) | RSS at `/cgi-bin/browse-edgar?action=getcurrent` | Polite cadence |
| CoinGecko | Crypto Fear & Greed, dominance | REST API | 30/min |

## New modules

- `src/trading_bot/intelligence.py` — feed aggregator
- `src/trading_bot/portfolio_monitor.py` — fill/stop/move detector
- `reports.py` extended with `build_rich_report_html()`

## New CLI commands

- `bot intel-scan` — light intelligence-driven scan (similar to full-run but condensed; logs only when actionable)
- `bot portfolio-watch` — fast portfolio change check (sends email only if material event)
- `bot rich-report --period mid|eod` — full HTML email with all data

## Acceptance

- All 4 schedules visible in `~/.claude/scheduled-tasks/`
- One manual run of each command succeeds against live Alpaca paper
- Rich report email contains: regime, positions+P&L, news per holding, macro snapshot (VIX, yields), decisions log
- Tests cover new modules (mocked HTTP)
