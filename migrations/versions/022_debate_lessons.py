"""debate_lessons table + prompt_version backfill on entry/unblock runs.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-05-02 21:00:00.000000+00:00

Phase D — Lesson Injection. A nightly analyzer (DebateOutcomeAnalyzerRole)
joins entry/unblock/hold debate runs with closed-trade outcomes (last 14
days) and writes a one-page lesson summary to ``debate_lessons``. The
scout/entry/hold debate briefs append the latest lesson under a "RECENT
LESSONS" heading so the next debate's reasoning is calibrated against
realised outcomes — in-context learning without prompt mutation.

Schema additions:
  - ``debate_lessons``: one row per nightly analysis. Lookback window,
    n_trades, summary text, per-source winrate JSON, candidate prompt
    edits JSON.
  - Backfill ``prompt_version`` on ``entry_debate_runs`` and
    ``unblock_debate_runs`` so all four debate-audit tables expose the
    same lineage column. (Scout + hold added it natively in earlier phases.)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'debate_lessons',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('analysis_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lookback_days', sa.Integer(), nullable=False),
        sa.Column('n_trades_closed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_entry_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_unblock_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('n_hold_debates', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('overall_place_winrate', sa.Float(), nullable=True),
        sa.Column('overall_skip_winrate', sa.Float(), nullable=True),
        sa.Column('summary_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('per_source_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('per_verdict_winrate_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('losing_patterns_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('shadow_skips_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('candidate_edits_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('prompt_version', sa.String(length=64), nullable=False, server_default=''),
    )
    op.create_index('ix_debate_lessons_analysis_date', 'debate_lessons', ['analysis_date'])

    # Backfill prompt_version on entry / unblock debate-run tables for
    # consistency. SQLite doesn't enforce ALTER TABLE column-add fully,
    # so we use batch_alter_table for portability.
    with op.batch_alter_table('entry_debate_runs') as batch:
        batch.add_column(sa.Column(
            'prompt_version', sa.String(length=64), nullable=False, server_default='',
        ))
    with op.batch_alter_table('unblock_debate_runs') as batch:
        batch.add_column(sa.Column(
            'prompt_version', sa.String(length=64), nullable=False, server_default='',
        ))


def downgrade() -> None:
    with op.batch_alter_table('unblock_debate_runs') as batch:
        batch.drop_column('prompt_version')
    with op.batch_alter_table('entry_debate_runs') as batch:
        batch.drop_column('prompt_version')
    op.drop_index('ix_debate_lessons_analysis_date', table_name='debate_lessons')
    op.drop_table('debate_lessons')
