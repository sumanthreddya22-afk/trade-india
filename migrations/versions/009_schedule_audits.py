"""schedule_audits table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-29 00:03:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'schedule_audits',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('audit_date', sa.Date(), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('expected_fires', sa.Integer(), nullable=False),
        sa.Column('actual_fires', sa.Integer(), nullable=False),
        sa.Column('ratio', sa.Float(), nullable=False),
        sa.Column('audited_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('audit_date', 'job_id'),
    )


def downgrade() -> None:
    op.drop_table('schedule_audits')
