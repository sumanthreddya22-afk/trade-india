"""yfinance adapter — free historical + option chains.

Why yfinance and not Alpaca:
  * Alpaca free plan blocks SIP data within the last 15 minutes
    (the "subscription does not permit querying recent SIP data" error).
  * Alpaca free plan does not include options chain data at all.
  * yfinance scrapes Yahoo Finance JSON; no key, no rate quota for
    reasonable use. Adjusted close handles splits + dividends.

Trade-offs:
  * yfinance is best-effort. Occasional empty responses on illiquid
    names. Wrap calls in try/except; never let yfinance failures crash
    the daemon.
  * Yahoo's options chain returns *current* chains only — no historical
    chain replay. For Tier-1 backtest of options strategies we must
    synthesise the chain (Black-Scholes against historical IV proxy).
  * Time resolution: daily bars are clean; intraday is rate-limited
    and Yahoo's intraday data is delayed 15 minutes.

Public surface:
  * ``fetch_daily_bars(symbols, start, end)`` → list[DailyBar]
  * ``fetch_option_chain(underlying, expiry)`` → ChainSnapshot
  * ``find_strike_by_delta(chain, target_delta, side)`` → float

Greeks are computed locally via Black-Scholes (``shared/black_scholes``).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from trading_bot.research.historical_bars import DailyBar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NSE / BSE symbol mapping
# ---------------------------------------------------------------------------
# yfinance needs an exchange suffix for Indian stocks:
#   NSE → ".NS"   (e.g. RELIANCE.NS, NIFTYBEES.NS)
#   BSE → ".BO"   (e.g. 500325.BO for Reliance on BSE)
# Crypto INR pairs use dash form: BTC-INR, ETH-INR
# Internally we store bare symbols (RELIANCE, BTC/INR).

def _to_yf_symbol(sym: str, *, exchange: str = "NSE") -> str:
    """Convert internal symbol → yfinance ticker."""
    # Crypto INR pairs: BTC/INR → BTC-INR
    if "/INR" in sym.upper():
        return sym.upper().replace("/", "-")
    # Crypto USD pairs (legacy): BTC/USD → BTC-USD
    if "/USD" in sym.upper():
        return sym.upper().replace("/", "-")
    # Already has suffix — pass through
    if sym.endswith((".NS", ".BO")):
        return sym
    # NSE index symbols that yfinance tracks with ^ prefix
    _INDEX_MAP = {
        "NIFTY": "^NSEI",
        "NIFTY50": "^NSEI",
        "BANKNIFTY": "^NSEBANK",
        "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
        "INDIAVIX": "^INDIAVIX",
    }
    upper = sym.upper()
    if upper in _INDEX_MAP:
        return _INDEX_MAP[upper]
    # Default: append .NS for NSE
    suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
    return f"{sym}{suffix}"


def _from_yf_symbol(yf_sym: str) -> str:
    """Convert yfinance ticker → internal symbol."""
    # Reverse index map
    _REVERSE_INDEX = {
        "^NSEI": "NIFTY",
        "^NSEBANK": "BANKNIFTY",
        "NIFTY_FIN_SERVICE.NS": "FINNIFTY",
        "^INDIAVIX": "INDIAVIX",
    }
    if yf_sym in _REVERSE_INDEX:
        return _REVERSE_INDEX[yf_sym]
    # Crypto: BTC-INR → BTC/INR
    if yf_sym.endswith("-INR") or yf_sym.endswith("-USD"):
        return yf_sym.replace("-", "/")
    # Strip exchange suffix
    for sfx in (".NS", ".BO"):
        if yf_sym.endswith(sfx):
            return yf_sym[:-len(sfx)]
    return yf_sym


# ---------------------------------------------------------------------------
# Daily bars
# ---------------------------------------------------------------------------

def fetch_daily_bars(
    *, symbols: Sequence[str], start: dt.date, end: dt.date,
    exchange: str = "NSE",
) -> list[DailyBar]:
    """Fetch adjusted daily bars for ``symbols`` between ``start`` and
    ``end`` (inclusive). Empty list on error.

    Handles NSE/BSE symbol mapping automatically — callers pass bare
    symbols (RELIANCE, NIFTYBEES) and get back bars with bare symbols.

    yfinance allows batch download; we use it to minimise round-trips.
    """
    if not symbols:
        return []
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed")
        return []

    # Build mapping: yf_ticker → internal_symbol
    yf_to_internal: dict[str, str] = {}
    yf_tickers: list[str] = []
    for sym in symbols:
        yf_sym = _to_yf_symbol(sym, exchange=exchange)
        yf_to_internal[yf_sym] = sym
        yf_tickers.append(yf_sym)

    try:
        # auto_adjust=True applies split + dividend adjustment to OHLC.
        # progress=False silences the progress bar in scripts.
        df = yf.download(
            tickers=yf_tickers,
            start=start.isoformat(),
            # yfinance excludes the end date; +1 day to include it.
            end=(end + dt.timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance.download failed: %s", e)
        return []

    if df is None or df.empty:
        return []

    out: list[DailyBar] = []
    multi = len(yf_tickers) > 1
    for yf_sym in yf_tickers:
        internal_sym = yf_to_internal[yf_sym]
        try:
            sub = df[yf_sym] if multi else df
            for ts, row in sub.iterrows():
                # ts is a pandas Timestamp (UTC-naive); date() is fine.
                bar_date = ts.date() if hasattr(ts, "date") else ts
                op = float(row.get("Open", 0) or 0)
                hi = float(row.get("High", 0) or 0)
                lo = float(row.get("Low", 0) or 0)
                cl = float(row.get("Close", 0) or 0)
                vol = float(row.get("Volume", 0) or 0)
                if cl <= 0 or math.isnan(cl):
                    continue
                out.append(DailyBar(
                    symbol=internal_sym, bar_date=bar_date,
                    open=op, high=hi, low=lo, close=cl, volume=vol,
                    vwap=None, source=f"yfinance:nse:1d:adj",
                ))
        except Exception:
            log.exception("yfinance row parse for %s (%s)", internal_sym, yf_sym)
            continue
    return out


# ---------------------------------------------------------------------------
# Option chains
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: dt.date
    strike: float
    side: str               # "call" | "put"
    bid: float
    ask: float
    last_price: float
    volume: float
    open_interest: float
    implied_volatility: float
    in_the_money: bool

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last_price


@dataclass(frozen=True)
class ChainSnapshot:
    underlying: str
    underlying_price: float
    fetched_at: dt.datetime
    expiry: dt.date
    calls: tuple[OptionContract, ...]
    puts: tuple[OptionContract, ...]


def list_expirations(underlying: str) -> list[dt.date]:
    """Return available option expirations for ``underlying`` as
    ``date`` objects, sorted ascending. Empty on error."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        tk = yf.Ticker(underlying)
        return sorted(
            dt.date.fromisoformat(e) for e in (tk.options or [])
        )
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance.options for %s failed: %s", underlying, e)
        return []


def fetch_option_chain(
    underlying: str, expiry: dt.date,
) -> Optional[ChainSnapshot]:
    """Fetch the option chain for ``underlying`` at ``expiry``. None on error."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        tk = yf.Ticker(underlying)
        # Underlying spot — pull last close from a 1d-history call.
        spot_hist = tk.history(period="1d")
        if spot_hist is None or spot_hist.empty:
            spot = 0.0
        else:
            spot = float(spot_hist["Close"].iloc[-1])
        oc = tk.option_chain(expiry.isoformat())
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance.option_chain %s@%s failed: %s",
                    underlying, expiry, e)
        return None

    def _row_to_contract(row, side: str) -> OptionContract:
        strike = float(row.get("strike", 0) or 0)
        return OptionContract(
            underlying=underlying, expiry=expiry, strike=strike, side=side,
            bid=float(row.get("bid", 0) or 0),
            ask=float(row.get("ask", 0) or 0),
            last_price=float(row.get("lastPrice", 0) or 0),
            volume=float(row.get("volume", 0) or 0),
            open_interest=float(row.get("openInterest", 0) or 0),
            implied_volatility=float(row.get("impliedVolatility", 0) or 0),
            in_the_money=bool(row.get("inTheMoney", False)),
        )

    calls = tuple(_row_to_contract(r, "call") for _, r in oc.calls.iterrows())
    puts = tuple(_row_to_contract(r, "put") for _, r in oc.puts.iterrows())
    return ChainSnapshot(
        underlying=underlying, underlying_price=spot,
        fetched_at=dt.datetime.now(dt.timezone.utc),
        expiry=expiry, calls=calls, puts=puts,
    )


# ---------------------------------------------------------------------------
# Strike selection by target delta
# ---------------------------------------------------------------------------

def find_contract_by_delta(
    chain: ChainSnapshot, *, side: str, target_delta: float,
    risk_free_rate: float = 0.045,
) -> Optional[OptionContract]:
    """Find the contract whose delta is closest to ``target_delta``.

    For puts, delta is negative; pass ``target_delta`` as a positive
    number (e.g. 0.30) and we compare absolute values. For calls, pass
    a positive number directly.

    Greeks computed locally via Black-Scholes — yfinance's chain doesn't
    surface delta directly. ``risk_free_rate`` is the assumed
    short-term rate (default 4.5% = recent 3-mo T-bill).
    """
    from trading_bot.shared.black_scholes import bs_delta

    contracts = chain.calls if side == "call" else chain.puts
    if not contracts or chain.underlying_price <= 0:
        return None

    today = dt.date.today()
    T = max((chain.expiry - today).days, 1) / 365.0

    best: Optional[OptionContract] = None
    best_diff: float = float("inf")
    for c in contracts:
        if c.implied_volatility <= 0 or c.strike <= 0:
            continue
        d = bs_delta(
            S=chain.underlying_price, K=c.strike, T=T,
            r=risk_free_rate, sigma=c.implied_volatility,
            option_type=side,
        )
        diff = abs(abs(d) - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = c
    return best


__all__ = [
    "ChainSnapshot", "OptionContract",
    "fetch_daily_bars", "fetch_option_chain",
    "find_contract_by_delta", "list_expirations",
]
