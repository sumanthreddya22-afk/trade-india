"""Phase 3 — options intel + debate audit + circuit breaker tables.

Revision ID: d0e1f2a3b4c5_ph3b
Revises: c9d0e1f2a3b4_ph3
Create Date: 2026-05-03 12:00:00.000000+00:00

Adds the options intel + debate plumbing parallel to crypto's tables:

  intel_events_options              — append-only options intel mentions
  intel_candidates_options          — score-decayed candidate roll-up
  scout_debate_runs_options         — Hank/Sofia/Marcus debate audit
  wheel_debate_runs_options         — Aurelio/Beatrice/Yusuf/Catherine debate audit
  debate_lessons_options            — nightly aggregated lessons
  circuit_breaker_events_options    — options breaker trip log
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd0e1f2a3b4c5_ph3b'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4_ph3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- intel_events_options -------------------------------------------
    op.create_table(
        'intel_events_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('headline', sa.Text(), nullable=False, server_default=''),
        sa.Column('url', sa.Text(), nullable=False, server_default=''),
        sa.Column('sentiment', sa.Float(), nullable=True),
        sa.Column('raw_score', sa.Float(), nullable=True),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('event_hash', sa.String(length=64), nullable=False, server_default=''),
        sa.UniqueConstraint(
            'underlying', 'source', 'event_hash',
            name='ux_intel_events_options_dedup',
        ),
    )
    op.create_index('ix_intel_events_options_underlying',
                    'intel_events_options', ['underlying'])
    op.create_index('ix_intel_events_options_source',
                    'intel_events_options', ['source'])
    op.create_index('ix_intel_events_options_ingested_at',
                    'intel_events_options', ['ingested_at'])

    # ----- intel_candidates_options ---------------------------------------
    op.create_table(
        'intel_candidates_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('n_mentions', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_sources', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('sources_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sentiment_avg', sa.Float(), nullable=True),
        sa.Column('rolled_up_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('iv_rank', sa.Float(), nullable=True),
        sa.Column('earnings_in_dte_window', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('days_to_earnings', sa.Integer(), nullable=True),
        sa.Column('cboe_skew', sa.Float(), nullable=True),
        sa.Column('scout_verdict', sa.String(length=16), nullable=True),
        sa.Column('scout_dismissed_until', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('underlying', name='ux_intel_candidates_options_underlying'),
    )
    op.create_index('ix_intel_candidates_options_score',
                    'intel_candidates_options', ['score'])
    op.create_index('ix_intel_candidates_options_last_seen',
                    'intel_candidates_options', ['last_seen'])
    op.create_index('ix_intel_candidates_options_scout_dismissed_until',
                    'intel_candidates_options', ['scout_dismissed_until'])

    # ----- scout_debate_runs_options --------------------------------------
    op.create_table(
        'scout_debate_runs_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('iv_rank', sa.Float(), nullable=True),
        sa.Column('earnings_in_dte_window', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('skeptic_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('analyst_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_scout_debate_runs_options_run_at',
                    'scout_debate_runs_options', ['run_at'])
    op.create_index('ix_scout_debate_runs_options_underlying',
                    'scout_debate_runs_options', ['underlying'])

    # ----- wheel_debate_runs_options --------------------------------------
    op.create_table(
        'wheel_debate_runs_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('underlying', sa.String(length=16), nullable=False),
        sa.Column('candidate_score', sa.Float(), nullable=True),
        sa.Column('iv_rank', sa.Float(), nullable=True),
        sa.Column('proposed_delta', sa.Float(), nullable=True),
        sa.Column('proposed_dte_days', sa.Integer(), nullable=True),
        sa.Column('proposed_strike', sa.Float(), nullable=True),
        sa.Column('regime', sa.String(length=32), nullable=True),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('chosen_delta', sa.Float(), nullable=True),
        sa.Column('chosen_dte_days', sa.Integer(), nullable=True),
        sa.Column('chosen_structure', sa.String(length=16), nullable=True),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('aggressive_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('conservative_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('neutral_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('entry_order_id', sa.String(length=64), nullable=True),
        sa.Column('cycle_id', sa.Integer(), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_wheel_debate_runs_options_run_at',
                    'wheel_debate_runs_options', ['run_at'])
    op.create_index('ix_wheel_debate_runs_options_underlying',
                    'wheel_debate_runs_options', ['underlying'])

    # ----- debate_lessons_options -----------------------------------------
    op.create_table(
        'debate_lessons_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('analysis_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lookback_days', sa.Integer(), nullable=False),
        sa.Column('n_cycles_closed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_wheel_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_scout_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('summary_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('per_source_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_iv_rank_band_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_dte_band_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_structure_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('candidate_prompt_edits_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
    )
    op.create_index('ix_debate_lessons_options_analysis_date',
                    'debate_lessons_options', ['analysis_date'])

    # ----- circuit_breaker_events_options ---------------------------------
    op.create_table(
        'circuit_breaker_events_options',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('tripped_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.String(length=48), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=False, server_default='warning'),
        sa.Column('trip_state_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('cooldown_minutes', sa.Integer(), nullable=False, server_default='60'),
    )
    op.create_index('ix_circuit_breaker_events_options_tripped_at',
                    'circuit_breaker_events_options', ['tripped_at'])


def downgrade() -> None:
    op.drop_index('ix_circuit_breaker_events_options_tripped_at',
                  table_name='circuit_breaker_events_options')
    op.drop_table('circuit_breaker_events_options')

    op.drop_index('ix_debate_lessons_options_analysis_date',
                  table_name='debate_lessons_options')
    op.drop_table('debate_lessons_options')

    op.drop_index('ix_wheel_debate_runs_options_underlying',
                  table_name='wheel_debate_runs_options')
    op.drop_index('ix_wheel_debate_runs_options_run_at',
                  table_name='wheel_debate_runs_options')
    op.drop_table('wheel_debate_runs_options')

    op.drop_index('ix_scout_debate_runs_options_underlying',
                  table_name='scout_debate_runs_options')
    op.drop_index('ix_scout_debate_runs_options_run_at',
                  table_name='scout_debate_runs_options')
    op.drop_table('scout_debate_runs_options')

    op.drop_index('ix_intel_candidates_options_scout_dismissed_until',
                  table_name='intel_candidates_options')
    op.drop_index('ix_intel_candidates_options_last_seen',
                  table_name='intel_candidates_options')
    op.drop_index('ix_intel_candidates_options_score',
                  table_name='intel_candidates_options')
    op.drop_table('intel_candidates_options')

    op.drop_index('ix_intel_events_options_ingested_at',
                  table_name='intel_events_options')
    op.drop_index('ix_intel_events_options_source',
                  table_name='intel_events_options')
    op.drop_index('ix_intel_events_options_underlying',
                  table_name='intel_events_options')
    op.drop_table('intel_events_options')
