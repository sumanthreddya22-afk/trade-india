"""Phase 3 — options pipeline state-machine tables.

Revision ID: c9d0e1f2a3b4_ph3
Revises: b8c9d0e1f2a3_ph1g2
Create Date: 2026-05-03 00:30:00.000000+00:00

Per Option 2: options owns its own tables. Three new tables modelling
the wheel state machine (cash → CSP → assigned → CC → called_away):

  contract_positions_options    — one row per open option contract
  wheel_cycles_options          — one row per wheel cycle (CSP → ... → exit)
  wheel_state_history_options   — append-only state transition log
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4_ph3'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3_ph1g2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- contract_positions_options -------------------------------------
    op.create_table(
        'contract_positions_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('option_type', sa.String(length=8), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('strike', sa.Float(), nullable=False),
        sa.Column('expiry', sa.DateTime(timezone=True), nullable=False),
        sa.Column('multiplier', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('avg_open_price', sa.Float(), nullable=False),
        sa.Column('open_order_id', sa.String(length=64), nullable=True),
        sa.Column('cycle_id', sa.Integer(), nullable=True),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('realized_pnl', sa.Float(), nullable=True),
        sa.UniqueConstraint(
            'underlying', 'option_type', 'strike', 'expiry', 'side',
            name='ux_contract_positions_options_dedup',
        ),
    )
    op.create_index('ix_contract_positions_options_underlying',
                    'contract_positions_options', ['underlying'])
    op.create_index('ix_contract_positions_options_expiry',
                    'contract_positions_options', ['expiry'])
    op.create_index('ix_contract_positions_options_cycle_id',
                    'contract_positions_options', ['cycle_id'])

    # ----- wheel_cycles_options -------------------------------------------
    op.create_table(
        'wheel_cycles_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('state', sa.String(length=24), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('initial_csp_strike', sa.Float(), nullable=True),
        sa.Column('assignment_share_basis', sa.Float(), nullable=True),
        sa.Column('final_called_away_at', sa.Float(), nullable=True),
        sa.Column('cumulative_premium', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('realized_pnl', sa.Float(), nullable=True),
        sa.Column('target_delta_csp', sa.Float(), nullable=True),
        sa.Column('target_delta_cc', sa.Float(), nullable=True),
    )
    op.create_index('ix_wheel_cycles_options_underlying',
                    'wheel_cycles_options', ['underlying'])

    # ----- wheel_state_history_options ------------------------------------
    op.create_table(
        'wheel_state_history_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('cycle_id', sa.Integer(), nullable=False),
        sa.Column('transitioned_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('from_state', sa.String(length=24), nullable=False),
        sa.Column('to_state', sa.String(length=24), nullable=False),
        sa.Column('transition', sa.String(length=48), nullable=False),
        sa.Column('details_json', sa.Text(), nullable=False, server_default='{}'),
    )
    op.create_index('ix_wheel_state_history_options_cycle_id',
                    'wheel_state_history_options', ['cycle_id'])
    op.create_index('ix_wheel_state_history_options_transitioned_at',
                    'wheel_state_history_options', ['transitioned_at'])


def downgrade() -> None:
    op.drop_index('ix_wheel_state_history_options_transitioned_at',
                  table_name='wheel_state_history_options')
    op.drop_index('ix_wheel_state_history_options_cycle_id',
                  table_name='wheel_state_history_options')
    op.drop_table('wheel_state_history_options')

    op.drop_index('ix_wheel_cycles_options_underlying',
                  table_name='wheel_cycles_options')
    op.drop_table('wheel_cycles_options')

    op.drop_index('ix_contract_positions_options_cycle_id',
                  table_name='contract_positions_options')
    op.drop_index('ix_contract_positions_options_expiry',
                  table_name='contract_positions_options')
    op.drop_index('ix_contract_positions_options_underlying',
                  table_name='contract_positions_options')
    op.drop_table('contract_positions_options')
