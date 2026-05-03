"""intel_stream_events + debate_queue tables (Phase G).

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-05-02 23:30:00.000000+00:00

Phase G — Event-Driven Ingestion. Two new tables:

  intel_stream_events — captured by EventStreamer (fast-poll SEC EDGAR /
    websocket-style sources). Distinct from intel_events because streaming
    sources fire express scout/hold debates immediately rather than
    waiting for the next ingestor tick. ``processed_at`` flips when the
    express handler has dispatched.

  debate_queue — priority-queue state for the priority-cap replacement.
    Candidates are queued with their score; the dispatcher consumes the
    top-N up to the daily cap. Demoted (deferred) rows roll over to the
    next tick rather than being silently dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b0c1d2e3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'intel_stream_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False, server_default=''),
        sa.Column('url', sa.Text(), nullable=False, server_default=''),
        sa.Column('sentiment', sa.Float(), nullable=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('event_hash', sa.String(length=64), nullable=False),
    )
    op.create_index(
        'ix_intel_stream_events_ingested_at',
        'intel_stream_events', ['ingested_at'],
    )
    op.create_index(
        'ix_intel_stream_events_symbol',
        'intel_stream_events', ['symbol'],
    )
    op.create_unique_constraint(
        'ux_intel_stream_events_hash',
        'intel_stream_events', ['source', 'event_hash'],
    )

    op.create_table(
        'debate_queue',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('debate_class', sa.String(length=16), nullable=False),  # entry|scout|hold
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('priority_score', sa.Float(), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('queued_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('outcome', sa.String(length=32), nullable=True),  # processed|demoted|expired
    )
    op.create_index('ix_debate_queue_class', 'debate_queue', ['debate_class'])
    op.create_index('ix_debate_queue_priority', 'debate_queue', ['priority_score'])
    op.create_index('ix_debate_queue_queued_at', 'debate_queue', ['queued_at'])


def downgrade() -> None:
    op.drop_index('ix_debate_queue_queued_at', table_name='debate_queue')
    op.drop_index('ix_debate_queue_priority', table_name='debate_queue')
    op.drop_index('ix_debate_queue_class', table_name='debate_queue')
    op.drop_table('debate_queue')
    op.drop_constraint('ux_intel_stream_events_hash', 'intel_stream_events', type_='unique')
    op.drop_index('ix_intel_stream_events_symbol', table_name='intel_stream_events')
    op.drop_index('ix_intel_stream_events_ingested_at', table_name='intel_stream_events')
    op.drop_table('intel_stream_events')
