# Watchlist

Symbols the bot may trade in Phase 1. Calibrated for the current paper account size.

## Stocks / ETFs
- **SPY** — S&P 500 ETF, broad market exposure
- **QQQ** — Nasdaq 100 ETF, tech-heavy growth
- **AAPL** — Apple, large-cap, highly liquid
- **MSFT** — Microsoft, large-cap, highly liquid
- **AMD** — AMD, mid-priced, semiconductor exposure

## Crypto
- **BTC/USD** — Bitcoin (fractional shares)
- **ETH/USD** — Ethereum (fractional shares)

## Sizing
- 5% concentration cap per symbol (~$750 at current $15k equity)
- 10% max position cap (~$1,500)
- 1% per-trade risk cap (~$150)

## Adding/Removing Symbols
Edit `watchlist.yaml`. The bot will pick up changes at the next scan.
