# SPDX-License-Identifier: Apache-2.0
"""add reject_counters — ingest refusals become countable

Rejections vanished into access logs, making "device gone quiet" and "oracle
refusing everything" indistinguishable from outside. One row per (UTC day,
reason), upserted per refusal — bounded by days x a handful of reasons, so a
hammering attacker grows a counter, not a table.

Revision ID: f3a17d84b209
Revises: e8b2a90c4f17
Create Date: 2026-08-11
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a17d84b209'
down_revision: str | None = 'e8b2a90c4f17'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'reject_counters',
        sa.Column('day_utc', sa.String(length=10), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('day_utc', 'reason'),
    )


def downgrade() -> None:
    op.drop_table('reject_counters')
