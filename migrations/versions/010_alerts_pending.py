"""alerts_pending + alerts_sent + bot_meta tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-29 00:04:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alerts_pending',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('queued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('detail_html', sa.Text(), nullable=False),
        sa.Column('dedup_key', sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('dedup_key'),
    )
    op.create_table(
        'alerts_sent',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('event_count', sa.Integer(), nullable=False),
        sa.Column('max_severity', sa.String(length=8), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'bot_meta',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('bot_meta')
    op.drop_table('alerts_sent')
    op.drop_table('alerts_pending')
