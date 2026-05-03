"""intel_candidates: add scout_verdict + scout_dismissed_until columns

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-05-02 19:00:00.000000+00:00

Phase B (Scout Debate): a 2-LLM committee + judge debates new high-score
candidates the moment they enter the intel pool. Verdicts are stored on
the candidate row itself so downstream consumers (orchestrator strategy
resolution, scanner) can filter dismissed symbols cheaply.

  scout_verdict          — 'elevate' | 'dismiss' | NULL (not yet debated)
  scout_dismissed_until  — TTL timestamp (NULL = not dismissed). The pool
                           reader filters WHERE scout_dismissed_until IS NULL
                           OR scout_dismissed_until < now(). When the TTL
                           expires the symbol is re-debatable.

The verdict is also persisted to a long-form audit table (scout_debate_runs)
written by the scout debate module — this column is the cheap-read version
the orchestrator + dashboard consult on every scan tick.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('intel_candidates') as batch:
        batch.add_column(sa.Column('scout_verdict', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('scout_dismissed_until', sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        'ix_intel_candidates_scout_dismissed_until',
        'intel_candidates',
        ['scout_dismissed_until'],
    )

    # Long-form audit log for scout debates (one row per debate tick).
    op.create_table(
        'scout_debate_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('skeptic_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('analyst_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('prompt_version', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )
    op.create_index('ix_scout_debate_runs_run_at', 'scout_debate_runs', ['run_at'])
    op.create_index('ix_scout_debate_runs_symbol', 'scout_debate_runs', ['symbol'])


def downgrade() -> None:
    op.drop_index('ix_scout_debate_runs_symbol', table_name='scout_debate_runs')
    op.drop_index('ix_scout_debate_runs_run_at', table_name='scout_debate_runs')
    op.drop_table('scout_debate_runs')
    op.drop_index('ix_intel_candidates_scout_dismissed_until', table_name='intel_candidates')
    with op.batch_alter_table('intel_candidates') as batch:
        batch.drop_column('scout_dismissed_until')
        batch.drop_column('scout_verdict')
