"""threshold_overrides: shadow-mode columns for Phase E adaptive thresholds.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-05-02 22:00:00.000000+00:00

Phase E — Adaptive Thresholds. Extends the existing threshold_overrides
table with shadow-mode columns so the tuner can run new values in
"what-if" mode for 14 days before flipping live. Live readers continue
to ignore shadow rows; the tuner promotes shadow → live only after
positive expected-value confirmation.

  shadow              — boolean: True = compute-only, do not feed live reads
  shadow_what_if_pnl  — float: backfilled by analyzer with the simulated
                        P&L delta versus the live value over the shadow
                        window
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('threshold_overrides') as batch:
        batch.add_column(sa.Column(
            'shadow', sa.Boolean(), nullable=False, server_default=sa.text('0'),
        ))
        batch.add_column(sa.Column(
            'shadow_what_if_pnl', sa.Float(), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table('threshold_overrides') as batch:
        batch.drop_column('shadow_what_if_pnl')
        batch.drop_column('shadow')
