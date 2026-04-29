"""trade_journal UNIQUE on entry_order_id + cleanup duplicates

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-29 00:01:00.000000+00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOTE: trades table lives in trade_journal.db (separate sqlite from state.db).
    # Alembic env.py points at state.db; this migration is no-op there.
    # The actual fix is in trade_journal.py (Steps 2.2+) using application-level
    # idempotency. We keep this migration as a placeholder for revision lineage.
    pass


def downgrade() -> None:
    pass
