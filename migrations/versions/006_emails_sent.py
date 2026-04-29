"""emails_sent table

Revision ID: a1b2c3d4e5f6
Revises: fb03c506f6b4
Create Date: 2026-04-29 00:00:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'fb03c506f6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'emails_sent',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('recipient', sa.Text(), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_emails_sent_sent_at'), 'emails_sent', ['sent_at'], unique=False)
    op.create_index(op.f('ix_emails_sent_kind'), 'emails_sent', ['kind'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_emails_sent_kind'), table_name='emails_sent')
    op.drop_index(op.f('ix_emails_sent_sent_at'), table_name='emails_sent')
    op.drop_table('emails_sent')
