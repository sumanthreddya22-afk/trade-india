"""OCC option symbol parse/format. Format: <UND><YYMMDD><C|P><STRIKE*1000 padded to 8>."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccContract:
    underlying: str
    expiration: dt.date
    kind: str  # "C" | "P"
    strike: float


def parse_occ(symbol: str) -> OccContract:
    m = _OCC_RE.match(symbol)
    if not m:
        raise ValueError(f"not an OCC contract: {symbol!r}")
    und, yymmdd, kind, strike8 = m.groups()
    expiration = dt.datetime.strptime(yymmdd, "%y%m%d").date()
    strike = int(strike8) / 1000.0
    return OccContract(underlying=und, expiration=expiration, kind=kind, strike=strike)


def format_occ(c: OccContract) -> str:
    yymmdd = c.expiration.strftime("%y%m%d")
    strike8 = f"{int(round(c.strike * 1000)):08d}"
    return f"{c.underlying}{yymmdd}{c.kind}{strike8}"
