# SPDX-License-Identifier: Apache-2.0
"""Node directory.

Phase 6.6 adds session-aware scoping:

  - GET /nodes : when an Authorization: Bearer <session> is presented,
    returns ONLY the nodes owned by the session's verified address.
    Without auth, returns every node — so the public dashboard's
    Trees-list view keeps working and so do existing tunnel viewers.
    Dashboard-side public-mode scrubbing already strips wallet_address
    in that path; this route doesn't double-scrub.

  - GET /nodes/{id} : when authed and the node belongs to a different
    operator, returns 404 (not 403) — don't leak existence of a Tree
    the session's wallet doesn't own.

  - DELETE /nodes/{id} : requires a session, owner-only. Cascades to
    readings + uptime_hours + attestations. 404 (again, not 403) on
    cross-operator attempts to keep the surface tight.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import models, sessions
from ..db import get_db
# Session deps are shared with /auth and /register so all three routes
# see identical 401 semantics. ``_maybe_session`` / ``_require_session``
# are alias names kept for the existing call sites in this module.
from ..session_deps import maybe_session as _maybe_session
from ..session_deps import require_session as _require_session

router = APIRouter()


class NodePublic(BaseModel):
    node_id: str
    wallet_address: str | None
    label: str | None
    fw_version: str | None
    registered_at: datetime
    last_seen_at: datetime | None
    last_reading_at: datetime | None
    # Phase 6.5: Pass binding. nft_id is the bech32 nft1... id of the
    # Orchard Pass the operator's wallet held at registration time;
    # pass_verified_at is when that verification ran. Both null on
    # legacy registrations (no wallet provided).
    pass_nft_id: str | None = None
    pass_verified_at: datetime | None = None


def _to_public(n: models.Node) -> NodePublic:
    return NodePublic(
        node_id=n.node_id,
        wallet_address=n.wallet_address,
        label=n.label,
        fw_version=n.fw_version,
        registered_at=n.registered_at,
        last_seen_at=n.last_seen_at,
        last_reading_at=n.last_reading_at,
        pass_nft_id=n.pass_nft_id,
        pass_verified_at=n.pass_verified_at,
    )


@router.get("/nodes", response_model=list[NodePublic])
def list_nodes(
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(_maybe_session),
) -> list[NodePublic]:
    """List nodes. With a session, scoped to that operator's wallet.
    Without, returns all (existing public-dashboard behavior)."""
    q = select(models.Node)
    if sess is not None:
        q = q.where(models.Node.wallet_address == sess.address)
    rows = db.execute(q.order_by(models.Node.registered_at.desc())).scalars().all()
    return [_to_public(n) for n in rows]


@router.get("/nodes/{node_id}", response_model=NodePublic)
def get_node(
    node_id: str,
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(_maybe_session),
) -> NodePublic:
    node = db.get(models.Node, node_id.upper())
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown node_id",
        )
    # Authenticated-but-wrong-owner returns 404 too. We don't want
    # operators able to enumerate other operators' trees by trial id.
    if sess is not None and node.wallet_address != sess.address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown node_id",
        )
    return _to_public(node)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node_id: str,
    db: Session = Depends(get_db),
    sess: sessions.Session = Depends(_require_session),
) -> None:
    """Delete a node and all its child rows. Owner-only.

    Cascades to readings, uptime_hours, and attestations via explicit
    deletes (we don't rely on SQLAlchemy ORM cascade because we're
    issuing raw DELETE statements for speed and to be sure FK-aware
    SQLite cleans up).
    """
    node_id = node_id.upper()
    node = db.get(models.Node, node_id)
    # 404 on both "doesn't exist" and "exists but not yours" so cross-
    # operator probes can't tell the difference.
    if node is None or node.wallet_address != sess.address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown node_id",
        )

    # Order matters: child tables before the parent. Otherwise the FK
    # constraints throw on platforms with enforcement on (SQLite OFF
    # by default; Postgres ON).
    db.execute(delete(models.Reading).where(
        models.Reading.node_id == node_id))
    db.execute(delete(models.UptimeHour).where(
        models.UptimeHour.node_id == node_id))
    db.execute(delete(models.Attestation).where(
        models.Attestation.node_id == node_id))
    db.delete(node)
    db.commit()
    return None
