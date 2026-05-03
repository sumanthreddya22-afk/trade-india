"""Phase 1B — crypto scout debate audit table.

Revision ID: c2d3e4f5a6b7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-02 21:30:00.000000+00:00

Adds the per-pipeline ``scout_debate_runs_crypto`` table. Mirrors the
shared ``scout_debate_runs`` shape from the stocks tree, minus the
``asset_class`` column (per-pipeline tables don't need it).

Inline UniqueConstraint pattern (per migration 026's note) — sidesteps
the SQLite ALTER-of-constraint limitation seen in migration 025.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scout_debate_runs_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('skeptic_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('analyst_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_scout_debate_runs_crypto_run_at',
        'scout_debate_runs_crypto', ['run_at'],
    )
    op.create_index(
        'ix_scout_debate_runs_crypto_symbol',
        'scout_debate_runs_crypto', ['symbol'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_scout_debate_runs_crypto_symbol',
        table_name='scout_debate_runs_crypto',
    )
    op.drop_index(
        'ix_scout_debate_runs_crypto_run_at',
        table_name='scout_debate_runs_crypto',
    )
    op.drop_table('scout_debate_runs_crypto')
