# SPDX-License-Identifier: Apache-2.0
"""Node directory.

Phase 6.6 adds session-aware scoping:

  - GET /nodes : when an Authorization: Bearer <session> is presented,
    returns ONLY the nodes owned by the session's verified address.
    Without auth, returns every node — so the public dashboard's
    Trees-list view keeps working and so do existing tunnel viewers.
    Public / cross-operator callers get wallet_address scrubbed to null
    at the oracle (defense-in-depth, so direct API consumers like the
    worldview globe can't tie a Tree to its operator's wallet); owners
    still see their own. Coarse geohash + sensor classes are public; the
    Pass binding stays public (it's an on-chain credential).

  - GET /nodes/{id} : when authed and the node belongs to a different
    operator, returns 404 (not 403) — don't leak existence of a Tree
    the session's wallet doesn't own.

  - DELETE /nodes/{id} : requires a session, owner-only. Cascades to
    readings + uptime_hours + attestations. 404 (again, not 403) on
    cross-operator attempts to keep the surface tight.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import audit, models, sessions
from ..db import get_db
from ..session_deps import LOOPBACK_HOSTS
# Session deps are shared with /auth and /register so all three routes
# see identical 401 semantics. ``_maybe_session`` / ``_require_session``
# are alias names kept for the existing call sites in this module.
from ..session_deps import maybe_session as _maybe_session
from ..session_deps import require_session as _require_session
from ..session_deps import writer_authenticated

router = APIRouter()


# Public location is intentionally COARSE: a ~5 km geohash cell (precision 5),
# derived from the node's latest GPS reading. Precise lat/lon stays operator-
# private (scrubbed in routes/readings.py); only the coarse cell is ever public.
_GEOHASH_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_GEOHASH_BITS = (16, 8, 4, 2, 1)


def _geohash_encode(lat: float, lon: float, precision: int = 5) -> str | None:
    """Standard geohash encode. Returns None for out-of-range coordinates."""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out: list[str] = []
    bit = 0
    ch = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon > mid:
                ch |= _GEOHASH_BITS[bit]
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat > mid:
                ch |= _GEOHASH_BITS[bit]
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_GEOHASH_B32[ch])
            bit = 0
            ch = 0
    return "".join(out)


class NodePublic(BaseModel):
    node_id: str
    label: str | None
    fw_version: str | None
    registered_at: datetime
    last_seen_at: datetime | None
    last_reading_at: datetime | None
    # ADR-0003: compressed secp256r1 pubkey — PUBLIC (required for anyone
    # to verify device-signed readings published to DataLayer).
    device_pubkey: str | None = None
    # Public, coarse: a ~5 km geohash cell + the node's sensor/data classes.
    # Shown to everyone — this is what the worldview globe renders.
    geohash: str | None = None
    # Where that cell came from, so a reader never has to guess whether a
    # position was measured or asserted:
    #   "device"   — derived from the Tree's own GPS reading
    #   "declared" — the operator said so, wallet-signed
    #   None       — the Tree is unplaced
    # Measured always wins over declared when both exist.
    location_source: str | None = None
    sensors: list[str] = []
    # wallet_address is operator-private: returned ONLY to the owning wallet
    # session (or for legacy unowned nodes), scrubbed to null for the public /
    # cross-operator callers so a Tree can't be tied to its operator's wallet.
    wallet_address: str | None = None
    # Pass binding stays PUBLIC: it's an on-chain credential the dashboard/site
    # surface. (Caveat: a Pass NFT resolves to its owner on-chain, so hiding the
    # wallet here is partial — see the worldview integration notes if full
    # operator anonymity is ever required.)
    pass_nft_id: str | None = None
    pass_verified_at: datetime | None = None


def _latest_reading(db: Session, node_id: str) -> models.Reading | None:
    return db.execute(
        select(models.Reading)
        .where(models.Reading.node_id == node_id)
        .order_by(models.Reading.received_at.desc())
        .limit(1)
    ).scalars().first()


def _to_public(
    n: models.Node,
    db: Session,
    sess: sessions.Session | None = None,
    *,
    writer: bool = False,
) -> NodePublic:
    # owner == legacy unowned node OR the session wallet matches the node's
    # wallet. Mirrors the GPS owner-check in routes/readings.py.
    #
    # ``writer`` widens this for the operator's own DataLayer writer / payout
    # process (authenticated by X-Orchard-Writer-Token, else loopback-only — the
    # same trust rule POST /attestations uses). The payout MUST be able to read
    # wallet_address to know where to send $JUICE; without this it resolved every
    # node to "" and skipped every payment while exiting 0.
    owner = writer or n.wallet_address is None or (
        sess is not None and sess.address == n.wallet_address
    )
    geohash: str | None = None
    location_source: str | None = None
    sensors: list[str] = []
    latest = _latest_reading(db, n.node_id)
    if latest is not None:
        # Coarse cell derived from precise GPS — public. Precise lat/lon
        # itself never leaves routes/readings.py (owner-only there).
        if latest.gps_lat is not None and latest.gps_lon is not None:
            geohash = _geohash_encode(latest.gps_lat, latest.gps_lon, 5)
            if geohash is not None:
                location_source = "device"
        try:
            s = json.loads(latest.payload_json).get("sensors")
            if isinstance(s, dict):
                sensors = sorted(s.keys())
        except (ValueError, TypeError):
            sensors = []
    # Fall back to what the operator declared. A Tree with no GPS module is
    # otherwise unplaceable — which is exactly how the globe went empty: the
    # only live Tree had no GPS and no entry in worldview's hardcoded table.
    # Measured beats asserted, so this never overrides a device fix.
    if geohash is None and n.declared_geohash:
        geohash = n.declared_geohash
        location_source = "declared"
    return NodePublic(
        node_id=n.node_id,
        label=n.label,
        fw_version=n.fw_version,
        registered_at=n.registered_at,
        last_seen_at=n.last_seen_at,
        last_reading_at=n.last_reading_at,
        device_pubkey=n.device_pubkey,
        geohash=geohash,
        location_source=location_source,
        sensors=sensors,
        wallet_address=n.wallet_address if owner else None,
        pass_nft_id=n.pass_nft_id,
        pass_verified_at=n.pass_verified_at,
    )


@router.get("/nodes", response_model=list[NodePublic])
def list_nodes(
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(_maybe_session),
    writer: bool = Depends(writer_authenticated),
    include_retired: bool = Query(default=False),
) -> list[NodePublic]:
    """List nodes. With a session, scoped to that operator's wallet.
    Without, returns all (existing public-dashboard behavior).

    Retired Trees are omitted unless ``include_retired=1``."""
    q = select(models.Node)
    # Retired Trees drop out of the living network. This ONE filter is what
    # removes them from the public map, the worldview globe, the DataLayer
    # publisher and the attestation writer — all four consume this endpoint.
    # ?include_retired=1 is for an operator auditing what was retired.
    if not include_retired:
        q = q.where(models.Node.retired_at.is_(None))
    if sess is not None:
        q = q.where(models.Node.wallet_address == sess.address)
    rows = db.execute(q.order_by(models.Node.registered_at.desc())).scalars().all()
    return [_to_public(n, db, sess, writer=writer) for n in rows]


@router.get("/nodes/{node_id}", response_model=NodePublic)
def get_node(
    node_id: str,
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(_maybe_session),
    writer: bool = Depends(writer_authenticated),
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
    return _to_public(node, db, sess, writer=writer)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node_id: str,
    request: Request,
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
    # Count children before deletion so the audit records the blast radius.
    readings_n = db.execute(
        select(models.Reading.id).where(models.Reading.node_id == node_id)
    ).scalars().all()
    db.execute(delete(models.Reading).where(
        models.Reading.node_id == node_id))
    db.execute(delete(models.UptimeHour).where(
        models.UptimeHour.node_id == node_id))
    db.execute(delete(models.Attestation).where(
        models.Attestation.node_id == node_id))
    # Audit BEFORE deleting the node row; AuditEvent has no FK to nodes so the
    # record survives the cascade. Committed atomically with the delete.
    audit.record(
        db,
        action="node.delete",
        node_id=node_id,
        actor=audit.actor_for(sess),
        request_id=audit.request_id_of(request),
        readings_deleted=len(readings_n),
        had_wallet=node.wallet_address is not None,
    )
    db.delete(node)
    db.commit()
    return None


class LocationAssert(BaseModel):
    """What an operator may say about where their own Tree is.

    Two ways to say it, because a person reading a map thinks in coordinates
    and a person reading the API thinks in cells:

      {"geohash": "dng01"}          — the cell directly
      {"lat": 25.77, "lon": -80.19} — coordinates, coarsened on arrival
      {"geohash": null}             — clear the declaration

    The coordinate form is a convenience, not a second precision tier. It is
    coarsened to a ~5 km cell before anything is written and the precise value
    is discarded in the same breath — it is never stored, logged, or returned.
    """
    geohash: str | None = None
    lat: float | None = None
    lon: float | None = None


# The privacy contract, in one number. Precision 5 is a ~4.9 km cell; that is
# the promise made on the worldview page and in ADR-0003, and it is enforced
# here rather than trusted, because a caller sending 9 characters is asking for
# metre-level publication whether they realise it or not.
_MAX_DECLARED_PRECISION = 5


def _clean_geohash(raw: str) -> str:
    g = raw.strip().lower()
    if not g:
        raise HTTPException(status_code=422, detail="geohash is empty")
    bad = sorted(set(g) - set(_GEOHASH_B32))
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"not a geohash — {''.join(bad)!r} is not in the base32 alphabet",
        )
    if len(g) > _MAX_DECLARED_PRECISION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(g)} characters is finer than this network publishes. "
                f"Locations are public at ~5 km ({_MAX_DECLARED_PRECISION} "
                f"characters); send '{g[:_MAX_DECLARED_PRECISION]}' or coarser."
            ),
        )
    return g


@router.post("/nodes/{node_id}/location", response_model=NodePublic)
def assert_node_location(
    node_id: str,
    body: LocationAssert,
    request: Request,
    db: Session = Depends(get_db),
    sess: sessions.Session = Depends(_require_session),
) -> NodePublic:
    """Declare where your own Tree is. Owner-only, wallet-authenticated.

    Most Trees have no GPS module, so without this they cannot appear on the
    map at all — not as an unknown position, but as nothing. This lets the
    operator answer on their own authority. It does not pretend to be a
    measurement: the response says ``location_source: "declared"``, and the
    moment the Tree reports a real GPS fix that measurement takes over.

    Reversible by design — send ``{"geohash": null}`` to withdraw it.
    """
    node_id = node_id.upper()
    node = db.get(models.Node, node_id)
    # 404 on both "doesn't exist" and "exists but not yours", matching DELETE:
    # a stranger must not be able to probe which node_ids are real.
    if node is None or node.wallet_address != sess.address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown node_id")

    has_coords = body.lat is not None or body.lon is not None
    if body.geohash and has_coords:
        raise HTTPException(
            status_code=422,
            detail="send either geohash or lat/lon, not both")

    if has_coords:
        if body.lat is None or body.lon is None:
            raise HTTPException(
                status_code=422, detail="lat and lon must be given together")
        cell = _geohash_encode(body.lat, body.lon, _MAX_DECLARED_PRECISION)
        if cell is None:
            raise HTTPException(
                status_code=422,
                detail="coordinates out of range (lat -90..90, lon -180..180)")
    elif body.geohash:
        cell = _clean_geohash(body.geohash)
    else:
        cell = None  # explicit withdrawal

    was = node.declared_geohash
    node.declared_geohash = cell
    node.declared_at = datetime.now(timezone.utc) if cell else None
    audit.record(
        db,
        action="node.location.declare" if cell else "node.location.clear",
        node_id=node_id,
        actor=audit.actor_for(sess),
        request_id=audit.request_id_of(request),
        # The cell is public by construction, so recording it leaks nothing.
        # Whatever precise coordinates the caller may have sent are already
        # gone and deliberately absent here.
        cell=cell,
        previous=was,
        via="coordinates" if has_coords else "geohash",
    )
    db.commit()
    db.refresh(node)
    return _to_public(node, db, sess)


class AuditEventOut(BaseModel):
    id: int
    ts: datetime
    action: str
    node_id: str | None
    actor: str
    request_id: str | None
    detail: dict


@router.get("/audit", response_model=list[AuditEventOut])
def list_audit(
    request: Request,
    node_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[AuditEventOut]:
    """Recent audit events, newest first. Loopback-only (the operator's own
    console) — the trail records wallet actors, so it is not public."""
    host = request.client.host if request.client else None
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="audit is loopback-only"
        )
    q = select(models.AuditEvent)
    if node_id is not None:
        q = q.where(models.AuditEvent.node_id == node_id.upper())
    rows = db.execute(
        q.order_by(models.AuditEvent.ts.desc(), models.AuditEvent.id.desc()).limit(limit)
    ).scalars().all()
    out: list[AuditEventOut] = []
    for r in rows:
        try:
            detail = json.loads(r.detail_json) if r.detail_json else {}
        except (ValueError, TypeError):
            detail = {}
        out.append(AuditEventOut(
            id=r.id, ts=r.ts, action=r.action, node_id=r.node_id,
            actor=r.actor, request_id=r.request_id, detail=detail,
        ))
    return out
