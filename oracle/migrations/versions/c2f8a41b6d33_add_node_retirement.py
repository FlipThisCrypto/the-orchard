# SPDX-License-Identifier: Apache-2.0
"""add nodes.retired_at + retired_reason

Re-flashing a board used to mint a NEW node_id, because the web installer
erased NVS where the identity lives. The oracle therefore accumulated ghost
Trees that no hardware will ever claim again — six registered ids for four
physical boards, confirmed by asking every board its NODE_ID over serial.

Deleting them was the wrong answer twice over: it destroys real history, and it
leaves DataLayer attestations — which are permanent and public — pointing at a
node_id the oracle would then deny exists. Retirement says "this is not part of
the living network" without ever claiming it never was.

Both columns are additive and nullable, so no existing row changes and the
downgrade is clean. NULL retired_at = live, which makes every existing Tree
live by default.

Revision ID: c2f8a41b6d33
Revises: b7c41d2e9a05
Create Date: 2026-08-09
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c2f8a41b6d33'
down_revision: str | None = 'b7c41d2e9a05'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('retired_reason', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.drop_column('retired_reason')
        batch_op.drop_column('retired_at')
