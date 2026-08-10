# SPDX-License-Identifier: Apache-2.0
"""add nodes.declared_geohash + declared_at

A Tree with no GPS module has no location at all, so it cannot appear on the
map — which is exactly what happened once the ghost Trees were retired and the
one remaining live Tree turned out to have neither a GPS reading nor an entry in
worldview's hardcoded fallback table. The globe went empty.

This lets an operator declare where their own Tree is, wallet-signed, as a
COARSE geohash cell. Device-reported GPS still wins when present — measured
beats asserted — and the public response says which it was.

Both columns additive and nullable.

Revision ID: d4e19c7a52f1
Revises: c2f8a41b6d33
Create Date: 2026-08-09
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e19c7a52f1'
down_revision: str | None = 'c2f8a41b6d33'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('declared_geohash', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('declared_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.drop_column('declared_at')
        batch_op.drop_column('declared_geohash')
