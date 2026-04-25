# Trading Bot

Semi-autonomous algorithmic trading bot for Alpaca paper account.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill in real values
```

## Usage

```bash
bot status      # Email current account status
bot dry-run --symbol AAPL --side buy --qty 10  # Simulate trade through risk manager
```

## Tests

```bash
pytest
```
