"""alert_dedup_persist — record dedup_key on alerts_sent so the dedup
window survives claim_pending+drain.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-05-02 01:30:00.000000+00:00

Without this, claim_pending() drains the alerts_pending queue (which
holds the dedup UNIQUE constraint), and the very next call to queue()
re-inserts the same dedup_key as a new row. Operator gets the same
"wheel skipped X" email every time the cron fires.

Adds dedup_key column to alerts_sent so AlertStore.queue() can check
"have we already sent this dedup_key in the last N hours?" before
accepting a new event.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'alerts_sent',
        sa.Column('dedup_key', sa.String(length=128), nullable=False, server_default=''),
    )
    op.create_index('ix_alerts_sent_dedup_key', 'alerts_sent', ['dedup_key'])


def downgrade() -> None:
    op.drop_index('ix_alerts_sent_dedup_key', table_name='alerts_sent')
    op.drop_column('alerts_sent', 'dedup_key')
