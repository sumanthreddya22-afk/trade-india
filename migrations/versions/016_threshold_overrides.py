"""threshold_overrides — adaptive risk/wheel/debate threshold overrides

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-05-02 02:30:00.000000+00:00

Phase 1 of the adaptive thresholds plan. This table holds per-knob
override values written by the nightly ``threshold_tuner`` lab role.
Read sites (risk_manager, wheel_lane, chain.py, orchestrator's
unblock-debate predicate) consult this table FIRST and fall back to the
static YAML config when no fresh override exists. Same freshness gate
shape as the wheel scout JSON: ``max_age_hours=36`` so a missed nightly
run silently degrades to static config.

One row per (knob, regime). New rows are appended; the read-side
``lookup()`` picks the most recent un-expired row. ``signal_summary``
captures the inputs the tuner saw (rolling win rate, IV percentile,
etc.) so the operator can audit *why* a knob moved.

``bounds_min`` / ``bounds_max`` are stored alongside the value as a
defense-in-depth: even if the lookup-side bound check is bypassed by a
buggy caller, the value persisted here is already clamped to the safe
range at write time.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'threshold_overrides',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('knob', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('regime', sa.String(length=32), nullable=True),
        sa.Column('bounds_min', sa.Float(), nullable=False),
        sa.Column('bounds_max', sa.Float(), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('set_by', sa.String(length=64), nullable=False),
        sa.Column('signal_summary', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'ix_threshold_overrides_knob', 'threshold_overrides', ['knob']
    )
    op.create_index(
        'ix_threshold_overrides_set_at', 'threshold_overrides', ['set_at']
    )
    op.create_index(
        'ix_threshold_overrides_knob_regime',
        'threshold_overrides', ['knob', 'regime'],
    )


def downgrade() -> None:
    op.drop_index('ix_threshold_overrides_knob_regime', table_name='threshold_overrides')
    op.drop_index('ix_threshold_overrides_set_at', table_name='threshold_overrides')
    op.drop_index('ix_threshold_overrides_knob', table_name='threshold_overrides')
    op.drop_table('threshold_overrides')
