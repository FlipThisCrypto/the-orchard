# SPDX-License-Identifier: Apache-2.0
"""add nodes.device_pubkey + audit_events (ADR-0003 / ADR-0007)

The DataLayer branch was written before this repo had Alembic, so its two
schema additions arrived only as SQLAlchemy models — `create_all()` produces
them on a fresh database, but the LIVE oracle's database has neither. Without
this migration, merging that branch to main means `/nodes` selects a
`device_pubkey` column that does not exist and the production oracle starts
erroring on its most-used endpoint.

Both changes are additive and touch no existing row:
  * `nodes.device_pubkey` — nullable, so every Tree registered before device
    keys existed stays loadable. The oracle learns the key from a Tree's first
    signed reading; until then it is NULL and the DataLayer publisher simply
    refuses to publish that node's hours as unverifiable.
  * `audit_events` — a new append-only table, deliberately with no foreign key
    to `nodes`, because a record of a deletion must outlive the node it
    describes.

Revision ID: b7c41d2e9a05
Revises: a002d3715ffb
Create Date: 2026-08-07
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c41d2e9a05'
down_revision: str | None = 'a002d3715ffb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Compressed secp256r1 public key: 33 bytes -> 66 lowercase hex chars.
    # Nullable on purpose — see module docstring.
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device_pubkey', sa.String(length=66), nullable=True))

    op.create_table(
        'audit_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('node_id', sa.String(length=64), nullable=True),
        sa.Column('actor', sa.String(length=128), nullable=False),
        sa.Column('request_id', sa.String(length=32), nullable=True),
        sa.Column('detail_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_events_ts'), ['ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_node_id'), ['node_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_node_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_action'))
        batch_op.drop_index(batch_op.f('ix_audit_events_ts'))
    op.drop_table('audit_events')

    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.drop_column('device_pubkey')
