"""hold_debate_runs + trade_intel_snapshots tables (Phase C)

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-05-02 20:00:00.000000+00:00

Phase C — Hold Debate (Debate #3): a 4-LLM committee fires for held
positions when the intel that drove the entry decays (intel_score drop,
sentiment flip, fresh adverse 8-K, etc.). Verdicts in {hold, tighten_stop,
exit_now} drive optional cancel-stop / flatten actions.

Two new tables:

  trade_intel_snapshots — one row per (entry_order_id, captured_at) snapshot.
    Captured at order placement so the hold debate has a stable baseline
    for "score dropped >50% from entry" comparisons. Keyed by
    entry_order_id (matches trade_journal). Stored in state.db (Alembic-
    managed) instead of mutating trade_journal's separate DB.

  hold_debate_runs — one row per hold-debate firing. Mirrors the shape of
    entry_debate_runs / scout_debate_runs so the dashboard + lessons loop
    can treat all debate tables uniformly.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trade_intel_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entry_order_id', sa.String(length=64), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('entry_intel_score', sa.Float(), nullable=True),
        sa.Column('entry_top_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('entry_sentiment_avg', sa.Float(), nullable=True),
        sa.Column('entry_top_sources_json', sa.Text(), nullable=False, server_default='[]'),
    )
    op.create_index(
        'ix_trade_intel_snapshots_entry_order_id',
        'trade_intel_snapshots', ['entry_order_id'],
    )
    op.create_index(
        'ix_trade_intel_snapshots_symbol',
        'trade_intel_snapshots', ['symbol'],
    )

    op.create_table(
        'hold_debate_runs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('asset_class', sa.String(length=16), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('entry_order_id', sa.String(length=64), nullable=True),
        sa.Column('trigger_reason', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('current_score', sa.Float(), nullable=True),
        sa.Column('current_sentiment', sa.Float(), nullable=True),
        sa.Column('entry_score', sa.Float(), nullable=True),
        sa.Column('entry_sentiment', sa.Float(), nullable=True),
        sa.Column('verdict', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.String(length=16), nullable=False),
        sa.Column('judge_reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('aggressive_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('conservative_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('neutral_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('action_taken', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('resulting_pnl_pct', sa.Float(), nullable=True),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('synthetic', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )
    op.create_index('ix_hold_debate_runs_run_at', 'hold_debate_runs', ['run_at'])
    op.create_index('ix_hold_debate_runs_symbol', 'hold_debate_runs', ['symbol'])
    op.create_index('ix_hold_debate_runs_entry_order_id', 'hold_debate_runs', ['entry_order_id'])


def downgrade() -> None:
    op.drop_index('ix_hold_debate_runs_entry_order_id', table_name='hold_debate_runs')
    op.drop_index('ix_hold_debate_runs_symbol', table_name='hold_debate_runs')
    op.drop_index('ix_hold_debate_runs_run_at', table_name='hold_debate_runs')
    op.drop_table('hold_debate_runs')
    op.drop_index('ix_trade_intel_snapshots_symbol', table_name='trade_intel_snapshots')
    op.drop_index('ix_trade_intel_snapshots_entry_order_id', table_name='trade_intel_snapshots')
    op.drop_table('trade_intel_snapshots')
