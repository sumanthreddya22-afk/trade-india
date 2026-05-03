"""Phase 1A — crypto pipeline owned intel tables.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-05-02 20:30:00.000000+00:00

Per Option 2 (three independent pipelines), crypto owns its own
intel tables. Same shape as the legacy ``intel_events`` /
``intel_candidates`` / ``intel_stream_events`` so the existing
aggregator + adversarial + scout patterns port over cleanly, but
distinct ``*_crypto`` table names so crypto data never mixes with
stocks data and crypto schema can evolve independently (e.g. add
crypto-native columns like ``chain``, ``tx_hash`` and the four
crypto-specific adversarial flags from Phase F.2).

Three new tables:

  intel_events_crypto          — append-only audit row per source mention
  intel_candidates_crypto      — aggregated candidate (one per symbol)
  intel_stream_events_crypto   — express-lane events (Whale Alert, Coinbase WS, etc.)

NOTE: All UniqueConstraints are declared inline inside ``create_table``
rather than via ``op.create_unique_constraint`` afterward. SQLite's
ALTER-of-constraint limitation prevents the alter-in-place pattern.
The earlier migrations in this tree (e.g. 025) used the alter-in-place
pattern and trip the same SQLite limitation when applied to a fresh
DB; we sidestep that here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b0c1d2e3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- intel_events_crypto --------------------------------------------
    op.create_table(
        'intel_events_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False, server_default=''),
        sa.Column('url', sa.Text(), nullable=False, server_default=''),
        sa.Column('sentiment', sa.Float(), nullable=True),
        sa.Column('raw_score', sa.Float(), nullable=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('chain', sa.String(length=16), nullable=True),
        sa.Column('tx_hash', sa.String(length=80), nullable=True),
        sa.UniqueConstraint(
            'symbol', 'source', 'event_hash',
            name='ux_intel_events_crypto_dedup',
        ),
    )
    op.create_index(
        'ix_intel_events_crypto_symbol',
        'intel_events_crypto', ['symbol'],
    )
    op.create_index(
        'ix_intel_events_crypto_source',
        'intel_events_crypto', ['source'],
    )
    op.create_index(
        'ix_intel_events_crypto_ingested_at',
        'intel_events_crypto', ['ingested_at'],
    )

    # ----- intel_candidates_crypto ----------------------------------------
    op.create_table(
        'intel_candidates_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('n_mentions', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_sources', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('sources_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sentiment_avg', sa.Float(), nullable=True),
        sa.Column('rolled_up_at', sa.DateTime(timezone=True), nullable=False),
        # Phase F shared adversarial flags
        sa.Column('dedup_url_hashes_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('suspicious_spike', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('coordinated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('pump_signature', sa.Boolean(), nullable=False, server_default=sa.false()),
        # Phase F.2 crypto-specific adversarial flags
        sa.Column('cold_start_token', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('whale_concentration', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('honeypot_detected', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sybil_coordinated', sa.Boolean(), nullable=False, server_default=sa.false()),
        # Phase B scout-debate verdict + dismissal TTL
        sa.Column('scout_verdict', sa.String(length=16), nullable=True),
        sa.Column('scout_dismissed_until', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('symbol', name='ux_intel_candidates_crypto_symbol'),
    )
    op.create_index(
        'ix_intel_candidates_crypto_score',
        'intel_candidates_crypto', ['score'],
    )
    op.create_index(
        'ix_intel_candidates_crypto_last_seen',
        'intel_candidates_crypto', ['last_seen'],
    )
    op.create_index(
        'ix_intel_candidates_crypto_dismissed_until',
        'intel_candidates_crypto', ['scout_dismissed_until'],
    )

    # ----- intel_stream_events_crypto -------------------------------------
    op.create_table(
        'intel_stream_events_crypto',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sentiment', sa.Float(), nullable=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('chain', sa.String(length=16), nullable=True),
        sa.Column('tx_hash', sa.String(length=80), nullable=True),
        sa.Column('event_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            'symbol', 'source', 'event_hash',
            name='ux_intel_stream_events_crypto_dedup',
        ),
    )
    op.create_index(
        'ix_intel_stream_events_crypto_symbol',
        'intel_stream_events_crypto', ['symbol'],
    )
    op.create_index(
        'ix_intel_stream_events_crypto_event_at',
        'intel_stream_events_crypto', ['event_at'],
    )
    op.create_index(
        'ix_intel_stream_events_crypto_processed_at',
        'intel_stream_events_crypto', ['processed_at'],
    )


def downgrade() -> None:
    # intel_stream_events_crypto
    op.drop_index(
        'ix_intel_stream_events_crypto_processed_at',
        table_name='intel_stream_events_crypto',
    )
    op.drop_index(
        'ix_intel_stream_events_crypto_event_at',
        table_name='intel_stream_events_crypto',
    )
    op.drop_index(
        'ix_intel_stream_events_crypto_symbol',
        table_name='intel_stream_events_crypto',
    )
    op.drop_table('intel_stream_events_crypto')

    # intel_candidates_crypto
    op.drop_index(
        'ix_intel_candidates_crypto_dismissed_until',
        table_name='intel_candidates_crypto',
    )
    op.drop_index(
        'ix_intel_candidates_crypto_last_seen',
        table_name='intel_candidates_crypto',
    )
    op.drop_index(
        'ix_intel_candidates_crypto_score',
        table_name='intel_candidates_crypto',
    )
    op.drop_table('intel_candidates_crypto')

    # intel_events_crypto
    op.drop_index(
        'ix_intel_events_crypto_ingested_at',
        table_name='intel_events_crypto',
    )
    op.drop_index(
        'ix_intel_events_crypto_source',
        table_name='intel_events_crypto',
    )
    op.drop_index(
        'ix_intel_events_crypto_symbol',
        table_name='intel_events_crypto',
    )
    op.drop_table('intel_events_crypto')
