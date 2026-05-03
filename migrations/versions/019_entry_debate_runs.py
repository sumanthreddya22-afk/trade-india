"""entry_debate_runs table — audit log for the pre-trade entry committee

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-05-02 18:00:00.000000+00:00

Phase 6: a 4-LLM committee debates every BUY signal that survives the
deterministic risk gate. One row written per debate (regardless of
verdict) so the audit trail is complete whether or not the order was
placed. Reconciler back-fills ``entry_order_id`` and ``closed_pnl_pct``
once the trade closes — that's what the decision_reflector joins on
when writing entry-class lessons.

Same shape as ``unblock_debate_runs`` so the dashboard + lessons loop
can treat both audit tables uniformly. Differs only in semantic columns:
``intel_score`` / ``signal_reason`` / ``regime`` instead of the unblock
table's ``overage_ratio`` / ``block_reason``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'entry_debate_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('intel_score', sa.Float(), nullable=True),
        sa.Column('signal_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('regime', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('aggressive_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('conservative_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('neutral_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('entry_order_id', sa.String(length=64), nullable=True),
        sa.Column('closed_pnl_pct', sa.Float(), nullable=True),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )
    op.create_index('ix_entry_debate_runs_run_at', 'entry_debate_runs', ['run_at'])
    op.create_index('ix_entry_debate_runs_asset_class', 'entry_debate_runs', ['asset_class'])
    op.create_index('ix_entry_debate_runs_symbol', 'entry_debate_runs', ['symbol'])
    op.create_index('ix_entry_debate_runs_entry_order_id', 'entry_debate_runs', ['entry_order_id'])


def downgrade() -> None:
    op.drop_index('ix_entry_debate_runs_entry_order_id', table_name='entry_debate_runs')
    op.drop_index('ix_entry_debate_runs_symbol', table_name='entry_debate_runs')
    op.drop_index('ix_entry_debate_runs_asset_class', table_name='entry_debate_runs')
    op.drop_index('ix_entry_debate_runs_run_at', table_name='entry_debate_runs')
    op.drop_table('entry_debate_runs')
