"""wheel strategy: option_fills, option_iv_history, wheel_cycles, wheel_universe_cache

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-28 12:00:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'option_fills',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('underlying', sa.String(length=16), nullable=False, index=True),
        sa.Column('contract_symbol', sa.String(length=32), nullable=False),
        sa.Column('option_type', sa.String(length=4), nullable=False),  # CSP|CC|ROLL
        sa.Column('side', sa.String(length=8), nullable=False),  # SELL|BUY
        sa.Column('strike', sa.Numeric(20, 4), nullable=False),
        sa.Column('expiration', sa.Date(), nullable=False),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('premium', sa.Numeric(20, 4), nullable=False),
        sa.Column('alpaca_order_id', sa.String(length=64), nullable=False),
        sa.Column('cycle_id', sa.String(length=64), nullable=True),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('alpaca_order_id'),
    )
    op.create_table(
        'option_iv_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('symbol', sa.String(length=16), nullable=False, index=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('atm_iv_30d', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'recorded_at', name='uq_iv_history_symbol_recorded'),
    )
    op.create_table(
        'wheel_cycles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cycle_id', sa.String(length=64), nullable=False, unique=True),
        sa.Column('symbol', sa.String(length=16), nullable=False, index=True),
        sa.Column('phase', sa.String(length=32), nullable=False),  # csp_open|assigned|cc_open|closed
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('csp_contract', sa.String(length=32), nullable=True),
        sa.Column('csp_strike', sa.Numeric(20, 4), nullable=True),
        sa.Column('csp_expiration', sa.Date(), nullable=True),
        sa.Column('csp_credit', sa.Numeric(20, 4), nullable=True),
        sa.Column('cc_contract', sa.String(length=32), nullable=True),
        sa.Column('cc_strike', sa.Numeric(20, 4), nullable=True),
        sa.Column('cc_expiration', sa.Date(), nullable=True),
        sa.Column('cc_credit', sa.Numeric(20, 4), nullable=True),
        sa.Column('rolls_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_basis', sa.Numeric(20, 4), nullable=True),
        sa.Column('realized_pnl', sa.Numeric(20, 4), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'wheel_universe_cache',
        sa.Column('symbol', sa.String(length=16), nullable=False),
        sa.Column('eligible', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('cached_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('symbol'),
    )


def downgrade() -> None:
    op.drop_table('wheel_universe_cache')
    op.drop_table('wheel_cycles')
    op.drop_table('option_iv_history')
    op.drop_table('option_fills')
