"""Phase 1G follow-on — crypto entry-debate audit table.

Revision ID: b8c9d0e1f2a3_ph1g2
Revises: a7b8c9d0e1f2_ph1e
Create Date: 2026-05-03 00:15:00.000000+00:00

Closes the per-pipeline architecture gap — crypto entry debates now
audit into ``entry_debate_runs_crypto`` instead of the shared
``entry_debate_runs`` (which stays stocks-only). Mirrors the
``ScoutDebateRunCrypto`` and ``HoldDebateRunCrypto`` shape with one
extra field — ``adjusted_qty`` — for when Diane Pereira sizes down
from Kai's proposed quantity based on Anya's risk concern.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8c9d0e1f2a3_ph1g2'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2_ph1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'entry_debate_runs_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('intel_top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('sentiment_avg', sa.Float(), nullable=True),
        sa.Column('regime', sa.String(length=32), nullable=True),
        sa.Column('proposed_qty', sa.Float(), nullable=True),
        sa.Column('proposed_entry_price', sa.Float(), nullable=True),
        sa.Column('proposed_stop_price', sa.Float(), nullable=True),
        sa.Column('proposed_target_price', sa.Float(), nullable=True),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('adjusted_qty', sa.Float(), nullable=True),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('aggressive_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('conservative_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('neutral_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('entry_order_id', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        'ix_entry_debate_runs_crypto_run_at',
        'entry_debate_runs_crypto', ['run_at'],
    )
    op.create_index(
        'ix_entry_debate_runs_crypto_symbol',
        'entry_debate_runs_crypto', ['symbol'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_entry_debate_runs_crypto_symbol',
        table_name='entry_debate_runs_crypto',
    )
    op.drop_index(
        'ix_entry_debate_runs_crypto_run_at',
        table_name='entry_debate_runs_crypto',
    )
    op.drop_table('entry_debate_runs_crypto')
