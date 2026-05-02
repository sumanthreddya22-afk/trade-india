"""unblock_debate_runs table — audit log for the unblock committee

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-05-02 00:45:00.000000+00:00

Phase 5: when a deterministic risk gate rejects a candidate (e.g. wheel
options_max_pct, sector_cap_pct, fallback flag short-circuit), an LLM
committee may debate whether to override. Every debate writes one row
here BEFORE the place attempt, so the audit trail is complete whether
the verdict was acted on or not. The reconciler back-fills
``entry_order_id`` and ``closed_pnl_pct`` once the trade closes — that's
the join the decision_reflector uses to write unblock-class lessons.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'unblock_debate_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('block_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('overage_ratio', sa.Float(), nullable=True),
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
    op.create_index('ix_unblock_debate_runs_run_at', 'unblock_debate_runs', ['run_at'])
    op.create_index('ix_unblock_debate_runs_asset_class', 'unblock_debate_runs', ['asset_class'])
    op.create_index('ix_unblock_debate_runs_symbol', 'unblock_debate_runs', ['symbol'])
    op.create_index('ix_unblock_debate_runs_entry_order_id', 'unblock_debate_runs', ['entry_order_id'])


def downgrade() -> None:
    op.drop_index('ix_unblock_debate_runs_entry_order_id', table_name='unblock_debate_runs')
    op.drop_index('ix_unblock_debate_runs_symbol', table_name='unblock_debate_runs')
    op.drop_index('ix_unblock_debate_runs_asset_class', table_name='unblock_debate_runs')
    op.drop_index('ix_unblock_debate_runs_run_at', table_name='unblock_debate_runs')
    op.drop_table('unblock_debate_runs')
