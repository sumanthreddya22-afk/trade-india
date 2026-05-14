"""Backtest hook — the integration point between mutation_engine and
real historical-data backtesting.

The mutation cycle calls a ``BacktestT(candidate) -> (p_value, sanity)``
callable for each Candidate. Phase 6 left this injected: tests use a
fake; production code must supply a real one before the operator
enables ``TRADING_BOT_ENABLE_MUTATION_CYCLE``.

This module ships:
  * ``StubBacktest`` — returns a deterministic p-value derived from a
    hash of the candidate's parameters. Useful for end-to-end smoke
    tests of the mutation pipeline without burning data.
  * ``ProductionBacktestT`` — the protocol the operator implements when
    real market data is plumbed in.

The stub is **not** suitable for any validation artifact. The mutation
engine's BH-FDR + DSR gates assume real backtest distributions; stubbing
those produces meaningless "survivors". The mutation cycle job refuses
to run the stub unless ``TRADING_BOT_ALLOW_STUB_BACKTEST=1`` is set.
"""
from __future__ import annotations

import hashlib
import os
import struct
from typing import Mapping, Protocol


class ProductionBacktestT(Protocol):
    """Operator-implemented protocol.

    Given a Candidate (which has ``family``, ``params``, ``code_hash``),
    returns the (raw_p_value, sanity_checks) pair.

    ``sanity_checks`` should include at least:
      * ``n_trades``
      * ``in_sample_sharpe``
      * ``out_of_sample_sharpe``
      * ``max_drawdown_pct``
      * ``data_window``
    """
    def __call__(self, candidate) -> tuple[float, Mapping]: ...


class StubBacktest:
    """Deterministic stub. Returns a fake p-value for plumbing tests
    only — never use for validation artifacts."""

    def __init__(self, *, allow: bool | None = None) -> None:
        if allow is None:
            allow = os.environ.get(
                "TRADING_BOT_ALLOW_STUB_BACKTEST", ""
            ).lower() in {"1", "true", "yes"}
        if not allow:
            raise RuntimeError(
                "StubBacktest is plumbing-only and is disabled by "
                "default. Set TRADING_BOT_ALLOW_STUB_BACKTEST=1 if you "
                "are running an end-to-end mutation cycle smoke test "
                "AND you understand that the results are meaningless."
            )

    def __call__(self, candidate) -> tuple[float, Mapping]:
        # Derive a stable p-value from the candidate id.
        cid = getattr(candidate, "candidate_id", str(candidate))
        h = hashlib.sha256(cid.encode("utf-8")).digest()
        # Map the first 4 bytes to [0, 1).
        raw = struct.unpack("<I", h[:4])[0] / (2**32)
        p = max(0.0001, min(0.9999, raw))
        return p, {
            "n_trades": 0, "in_sample_sharpe": 0.0,
            "out_of_sample_sharpe": 0.0, "max_drawdown_pct": 0.0,
            "data_window": "stub", "_note": "stub: not for validation",
        }


__all__ = ["ProductionBacktestT", "StubBacktest"]
