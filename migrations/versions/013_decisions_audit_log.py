"""decisions table — append-only audit log of every Decision the bot makes

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-29 20:00:00.000000+00:00

W1.2 of the PDF-parity plan. Stores the full PDF strict JSON contract
(decision, risk_after, compliance, data_quality, execution_constraints,
alerts, audit) for every decision — placed orders AND rejections AND
skips AND escalations. JSON columns hold the sub-objects; the columns
that show up in dashboards/queries are top-level (symbol, action,
strategy, regime, etc.).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'decisions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('decision_id', sa.String(length=64), nullable=False),
        sa.Column('timestamp_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('symbol', sa.String(length=32), nullable=False),
        sa.Column('action', sa.String(length=48), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('strategy', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('regime', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('asset_class', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('expected_edge_bps', sa.Float(), nullable=True),
        sa.Column('risk_after_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('compliance_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('data_quality_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('execution_constraints_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('alerts_json', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('audit_json', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('entry_order_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('stop_loss_order_id', sa.String(length=64), nullable=False, server_default=''),
        sa.UniqueConstraint('decision_id', name='uq_decisions_decision_id'),
    )
    op.create_index('ix_decisions_timestamp_utc', 'decisions', ['timestamp_utc'])
    op.create_index('ix_decisions_symbol', 'decisions', ['symbol'])
    op.create_index('ix_decisions_action', 'decisions', ['action'])
    op.create_index('ix_decisions_strategy', 'decisions', ['strategy'])


def downgrade() -> None:
    op.drop_index('ix_decisions_strategy', table_name='decisions')
    op.drop_index('ix_decisions_action', table_name='decisions')
    op.drop_index('ix_decisions_symbol', table_name='decisions')
    op.drop_index('ix_decisions_timestamp_utc', table_name='decisions')
    op.drop_table('decisions')
