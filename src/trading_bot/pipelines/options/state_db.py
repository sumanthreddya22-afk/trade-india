"""Options pipeline — owned tables (Phase 3 scaffold).

Per Option 2: options owns its own tables. The options domain has
two structurally-novel concepts compared to stocks/crypto:

  1. **Contract-level positions** — one ``ContractPositionOptions`` row
     per (underlying, option_type, strike, expiry). A trader can hold
     multiple contracts on the same underlying simultaneously
     (e.g. long the $180 CSP, short the $190 CC).

  2. **Wheel state machine** — ``WheelCycle`` carries the current state:
     ``cash → CSP → assigned → CC → called_away → cash``. Each transition
     writes a new state-history row so the audit trail captures the
     full cycle. The wheel cycle joins to ``ContractPositionOptions``
     via ``cycle_id``.

These tables coexist with the legacy ``trading_bot.state_db.WheelCycles``
table (which lives on the shared schema). Phase 3 reads from this new
per-pipeline table; Phase 2 stocks-extraction can later relocate the
legacy one if needed.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint,
)

from trading_bot.state_db import Base


class ContractPositionOptions(Base):
    """One open option contract position.

    Multiple rows can share an ``underlying`` — that's intentional, the
    wheel can hold a CSP at one strike + a CC at another strike on the
    same underlying simultaneously.
    """
    __tablename__ = "contract_positions_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False, index=True)
    option_type = Column(String(8), nullable=False)        # call | put
    side = Column(String(8), nullable=False)               # long | short
    strike = Column(Float, nullable=False)
    expiry = Column(DateTime(timezone=True), nullable=False, index=True)
    multiplier = Column(Integer, nullable=False, default=100)
    qty = Column(Integer, nullable=False)                  # contracts (signed)
    avg_open_price = Column(Float, nullable=False)         # per-share, NOT per-contract
    open_order_id = Column(String(64), nullable=True)
    cycle_id = Column(Integer, nullable=True, index=True)  # join to WheelCycleOptions
    opened_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    realized_pnl = Column(Float, nullable=True)            # backfilled on close
    __table_args__ = (
        UniqueConstraint(
            "underlying", "option_type", "strike", "expiry", "side",
            name="ux_contract_positions_options_dedup",
        ),
    )


class WheelCycleOptions(Base):
    """One wheel cycle from initial CSP entry through called-away exit.

    ``state`` is the current state machine position. State transitions
    are append-only via ``WheelStateHistoryOptions`` so the audit trail
    captures every step (cash → CSP at strike X, premium $Y → CSP
    expired worthless, premium kept → ...).
    """
    __tablename__ = "wheel_cycles_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    underlying = Column(String(16), nullable=False, index=True)
    state = Column(String(24), nullable=False)             # cash | csp_open | assigned | cc_open | called_away
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    initial_csp_strike = Column(Float, nullable=True)
    assignment_share_basis = Column(Float, nullable=True)  # cost basis on assignment
    final_called_away_at = Column(Float, nullable=True)    # exit strike on assignment
    cumulative_premium = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=True)            # backfilled on cycle close
    target_delta_csp = Column(Float, nullable=True)
    target_delta_cc = Column(Float, nullable=True)


class WheelStateHistoryOptions(Base):
    """Append-only state-transition log for one wheel cycle.

    Every state change in WheelCycleOptions writes a row here so the
    audit trail is reconstructable. ``transition`` describes the move
    (e.g. ``cash → csp_open``, ``csp_open → assigned``).
    """
    __tablename__ = "wheel_state_history_options"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cycle_id = Column(Integer, nullable=False, index=True)
    transitioned_at = Column(DateTime(timezone=True), nullable=False, index=True)
    from_state = Column(String(24), nullable=False)
    to_state = Column(String(24), nullable=False)
    transition = Column(String(48), nullable=False)
    details_json = Column(Text, nullable=False, default="{}")
