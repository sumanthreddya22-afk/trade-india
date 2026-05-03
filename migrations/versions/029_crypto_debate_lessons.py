"""Phase 1D — crypto debate lessons table.

Revision ID: e5f6a7b8c9d0_ph1d
Revises: d4e5f6a7b8c9_ph1c
Create Date: 2026-05-02 23:00:00.000000+00:00

Per Option 2 (independent pipelines), crypto owns its own lessons
table — distinct from the shared ``debate_lessons`` so stocks lessons
don't leak into crypto debate briefs (and vice versa). Schema mirrors
the shared one but adds two crypto-native attribution dimensions:
``per_chain_winrate_json`` and ``per_funding_band_winrate_json``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0_ph1d'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9_ph1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'debate_lessons_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('analysis_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lookback_days', sa.Integer(), nullable=False),
        sa.Column('n_trades_closed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_hold_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_scout_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('summary_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('per_source_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_trigger_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_chain_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_funding_band_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('candidate_prompt_edits_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
    )
    op.create_index(
        'ix_debate_lessons_crypto_analysis_date',
        'debate_lessons_crypto', ['analysis_date'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_debate_lessons_crypto_analysis_date',
        table_name='debate_lessons_crypto',
    )
    op.drop_table('debate_lessons_crypto')
