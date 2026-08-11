# SPDX-License-Identifier: Apache-2.0
"""add uptime_hours.slots_mask — the heartbeat-burst defense

reading_count says how much arrived in an hour; slots_mask (a bitmask of the
hour's six ten-minute slots) says whether it SPANNED the hour. A device that
wakes, bursts 30 readings in two minutes and sleeps fills the quorum but sets
one bit — and "heartbeat bursts pretending to represent hourly uptime" is on
the tokenomics anti-gaming list by name.

NOT NULL DEFAULT 0 is safe: pre-existing rows read as spread-unknown, which
the credit rule exempts — a rule cannot be applied retroactively to data that
never recorded spread.

Revision ID: e8b2a90c4f17
Revises: d4e19c7a52f1
Create Date: 2026-08-10
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e8b2a90c4f17'
down_revision: str | None = 'd4e19c7a52f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('uptime_hours', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'slots_mask', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('uptime_hours', schema=None) as batch_op:
        batch_op.drop_column('slots_mask')
