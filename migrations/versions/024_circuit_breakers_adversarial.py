"""circuit_breaker_events + adversarial flags on intel_candidates (Phase F).

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-05-02 23:00:00.000000+00:00

Phase F — Circuit Breakers + Adversarial Defense:

  1. ``circuit_breaker_events`` audit table — one row per trip / clear
     event so we can see when entries were frozen and why.

  2. New columns on ``intel_candidates`` for adversarial flags computed
     by the new ``adversarial`` module before scoring:
       - dedup_url_hashes (JSON list)
       - suspicious_spike (bool — cold-start mention spike >10x)
       - coordinated (bool — 3+ near-identical headlines in 5 min)
       - pump_signature (bool — small-cap + WSB spike + neutral news)

These flags are surfaced to the scout debate brief and used to bias
verdicts toward dismiss when adversarial signatures appear.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, Sequence[str], None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'circuit_breaker_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),  # tripped | cleared
        sa.Column('reason', sa.String(length=64), nullable=False),
        sa.Column('detail_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_circuit_breaker_events_event_at', 'circuit_breaker_events', ['event_at'])

    with op.batch_alter_table('intel_candidates') as batch:
        batch.add_column(sa.Column(
            'dedup_url_hashes_json', sa.Text(), nullable=False, server_default='[]',
        ))
        batch.add_column(sa.Column(
            'suspicious_spike', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))
        batch.add_column(sa.Column(
            'coordinated', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))
        batch.add_column(sa.Column(
            'pump_signature', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('intel_candidates') as batch:
        batch.drop_column('pump_signature')
        batch.drop_column('coordinated')
        batch.drop_column('suspicious_spike')
        batch.drop_column('dedup_url_hashes_json')
    op.drop_index('ix_circuit_breaker_events_event_at', table_name='circuit_breaker_events')
    op.drop_table('circuit_breaker_events')
