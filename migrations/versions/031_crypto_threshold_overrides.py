"""Phase 1E — crypto threshold-overrides table.

Revision ID: a7b8c9d0e1f2_ph1e
Revises: f6a7b8c9d0e1_ph1f
Create Date: 2026-05-02 23:45:00.000000+00:00

Per Option 2: crypto owns its own threshold_overrides_crypto table —
distinct from the shared ``threshold_overrides`` so a stocks ``sideways``
override never collides with a crypto ``crypto_range`` override.

Includes the shadow-mode columns (``shadow``, ``shadow_what_if_pnl``,
``promoted_to_live_at``) so the tuner can experiment for 14 days
before flipping a proposal to live.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2_ph1e'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1_ph1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'threshold_overrides_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('knob', sa.String(length=64), nullable=False),
        sa.Column('regime', sa.String(length=32), nullable=True),
        sa.Column('proposed_value', sa.Float(), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False, server_default=''),
        sa.Column('proposed_by', sa.String(length=64), nullable=False, server_default='threshold_tuner'),
        sa.Column('proposed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('shadow', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('shadow_what_if_pnl', sa.Float(), nullable=True),
        sa.Column('promoted_to_live_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_threshold_overrides_crypto_knob',
        'threshold_overrides_crypto', ['knob'],
    )
    op.create_index(
        'ix_threshold_overrides_crypto_regime',
        'threshold_overrides_crypto', ['regime'],
    )
    op.create_index(
        'ix_threshold_overrides_crypto_proposed_at',
        'threshold_overrides_crypto', ['proposed_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_threshold_overrides_crypto_proposed_at',
        table_name='threshold_overrides_crypto',
    )
    op.drop_index(
        'ix_threshold_overrides_crypto_regime',
        table_name='threshold_overrides_crypto',
    )
    op.drop_index(
        'ix_threshold_overrides_crypto_knob',
        table_name='threshold_overrides_crypto',
    )
    op.drop_table('threshold_overrides_crypto')
