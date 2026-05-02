"""events_bus — durable, append-only event bus for real-time dashboard

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-05-02 12:00:00.000000+00:00

Single ``events`` table that every launchd process (daemon / lab /
supervisor / mailbox / dashboard) writes to via
``trading_bot.event_bus.bus.emit``. The dashboard SSE endpoint tails
this table by ``id > cursor`` and broadcasts to connected browser
clients.

Schema is intentionally minimal — payload is a JSON blob; price ticks
do NOT go in here (they have their own ephemeral in-process channel).
Retention is enforced by a nightly DELETE + WAL checkpoint truncate.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('type', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('source', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('process', sa.String(length=16), nullable=False, server_default='unknown'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    # Tail by id is the hot read path.
    op.create_index('ix_events_id', 'events', ['id'])
    op.create_index('ix_events_type', 'events', ['type'])
    op.create_index('ix_events_created_at', 'events', ['created_at'])
    # Filtered debug queries by (type, created_at).
    op.create_index('ix_events_type_created_at', 'events', ['type', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_events_type_created_at', table_name='events')
    op.drop_index('ix_events_created_at', table_name='events')
    op.drop_index('ix_events_type', table_name='events')
    op.drop_index('ix_events_id', table_name='events')
    op.drop_table('events')
