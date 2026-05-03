"""Phase 1F — crypto circuit breaker events table.

Revision ID: f6a7b8c9d0e1_ph1f
Revises: e5f6a7b8c9d0_ph1d
Create Date: 2026-05-02 23:30:00.000000+00:00

Per Option 2: crypto owns its own circuit_breaker_events table —
distinct from the shared ``circuit_breaker_events`` so a stock VIX
trip doesn't block crypto and vice versa.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1_ph1f'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0_ph1d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'circuit_breaker_events_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tripped_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.String(length=48), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False, server_default='warning'),
        sa.Column('trip_state_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('cooldown_minutes', sa.Integer(), nullable=False, server_default='30'),
    )
    op.create_index(
        'ix_circuit_breaker_events_crypto_tripped_at',
        'circuit_breaker_events_crypto', ['tripped_at'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_circuit_breaker_events_crypto_tripped_at',
        table_name='circuit_breaker_events_crypto',
    )
    op.drop_table('circuit_breaker_events_crypto')
