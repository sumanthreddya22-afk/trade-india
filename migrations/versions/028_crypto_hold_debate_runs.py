"""Phase 1C — crypto hold debate audit table.

Revision ID: c3d4e5f6a7b8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-02 22:30:00.000000+00:00

Adds the per-pipeline ``hold_debate_runs_crypto`` table. Mirrors the
shared ``hold_debate_runs`` shape from the stocks tree, minus the
``asset_class`` column (per-pipeline tables don't need it).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9_ph1c'
down_revision: Union[str, Sequence[str], None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'hold_debate_runs_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('entry_order_id', sa.String(length=64), nullable=True),
        sa.Column('trigger_reason', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('current_score', sa.Float(), nullable=True),
        sa.Column('current_sentiment', sa.Float(), nullable=True),
        sa.Column('entry_score', sa.Float(), nullable=True),
        sa.Column('entry_sentiment', sa.Float(), nullable=True),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('aggressive_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('conservative_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('neutral_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('action_taken', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('resulting_pnl_pct', sa.Float(), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_hold_debate_runs_crypto_run_at',
        'hold_debate_runs_crypto', ['run_at'],
    )
    op.create_index(
        'ix_hold_debate_runs_crypto_symbol',
        'hold_debate_runs_crypto', ['symbol'],
    )
    op.create_index(
        'ix_hold_debate_runs_crypto_entry_order_id',
        'hold_debate_runs_crypto', ['entry_order_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_hold_debate_runs_crypto_entry_order_id',
        table_name='hold_debate_runs_crypto',
    )
    op.drop_index(
        'ix_hold_debate_runs_crypto_symbol',
        table_name='hold_debate_runs_crypto',
    )
    op.drop_index(
        'ix_hold_debate_runs_crypto_run_at',
        table_name='hold_debate_runs_crypto',
    )
    op.drop_table('hold_debate_runs_crypto')
