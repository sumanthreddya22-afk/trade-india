"""intel_pool — continuous internet-driven candidate pool

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-02 11:00:00.000000+00:00

Replaces the static ``CORE_LIQUID_TICKERS``/wheel-allowlist universe sources
with an aggregated, score-decayed pool of symbols continuously surfaced from
news, filings, social, and macro feeds. Two tables:

  * ``intel_events`` — append-only audit. Every news/filing/mention recorded
    once with source, sentiment, raw URL, headline. Lets the operator
    explain why a candidate scored where it did.

  * ``intel_candidates`` — materialized aggregate. One row per
    (symbol, asset_class) — score, n_mentions, last_seen, top_reason,
    sources_json. Read by the daemon at the START of each scan
    (preferred over opportunities.md / scout JSON / allowlist).

Hot path consumes from ``intel_candidates``; the events table is purely
audit. Both have generous indexes for the dashboard view + scoring loop.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'intel_events',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False, server_default=''),
        sa.Column('url', sa.Text(), nullable=False, server_default=''),
        sa.Column('sentiment', sa.Float(), nullable=True),
        sa.Column('raw_score', sa.Float(), nullable=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_hash', sa.String(length=64), nullable=False, server_default=''),
    )
    op.create_index('ix_intel_events_symbol', 'intel_events', ['symbol'])
    op.create_index('ix_intel_events_ingested_at', 'intel_events', ['ingested_at'])
    op.create_index('ix_intel_events_source', 'intel_events', ['source'])
    # Dedup: same (symbol, source, event_hash) shouldn't insert twice. The
    # ingester computes event_hash from (source, url) or (source, headline)
    # so a re-fetch of the same article is a no-op.
    op.create_index(
        'ux_intel_events_dedup', 'intel_events',
        ['symbol', 'source', 'event_hash'], unique=True,
    )

    op.create_table(
        'intel_candidates',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('n_mentions', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_sources', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('sources_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sentiment_avg', sa.Float(), nullable=True),
        sa.Column('rolled_up_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_intel_candidates_score', 'intel_candidates', ['score'])
    op.create_index('ix_intel_candidates_asset_class', 'intel_candidates', ['asset_class'])
    op.create_index('ix_intel_candidates_last_seen', 'intel_candidates', ['last_seen'])
    op.create_index(
        'ux_intel_candidates_symbol_class', 'intel_candidates',
        ['symbol', 'asset_class'], unique=True,
    )


def downgrade() -> None:
    op.drop_index('ux_intel_candidates_symbol_class', table_name='intel_candidates')
    op.drop_index('ix_intel_candidates_last_seen', table_name='intel_candidates')
    op.drop_index('ix_intel_candidates_asset_class', table_name='intel_candidates')
    op.drop_index('ix_intel_candidates_score', table_name='intel_candidates')
    op.drop_table('intel_candidates')

    op.drop_index('ux_intel_events_dedup', table_name='intel_events')
    op.drop_index('ix_intel_events_source', table_name='intel_events')
    op.drop_index('ix_intel_events_ingested_at', table_name='intel_events')
    op.drop_index('ix_intel_events_symbol', table_name='intel_events')
    op.drop_table('intel_events')
