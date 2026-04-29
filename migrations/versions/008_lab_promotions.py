"""lab_promotions table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-29 00:02:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lab_promotions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('promoted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('version', sa.String(length=64), nullable=False),
        sa.Column('template', sa.String(length=32), nullable=False),
        sa.Column('git_sha', sa.String(length=64), nullable=False),
        sa.Column('fitness_at_promotion', sa.Float(), nullable=False),
        sa.Column('params_json', sa.Text(), nullable=False),
        sa.Column('risk_caps_json', sa.Text(), nullable=False),
        sa.Column('scans_since_promote', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('entries_since_promote', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('near_misses_since_promote', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('version'),
    )
    op.create_index(op.f('ix_lab_promotions_promoted_at'), 'lab_promotions',
                    ['promoted_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_lab_promotions_promoted_at'), table_name='lab_promotions')
    op.drop_table('lab_promotions')
