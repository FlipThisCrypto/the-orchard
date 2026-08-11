# SPDX-License-Identifier: Apache-2.0
"""On-chain attestation records.

The Phase 5 writer (``orchard_chia.datalayer.main``) signs each Tree's
per-Season uptime, writes it to a Chia DataLayer store, and POSTs the
resulting tx_id + key here so the oracle's local DB tracks what's on
chain. The dashboard reads from this table to render the "On chain"
card on each Tree's live view — no DataLayer round-trip per render.

Endpoints:
  POST /attestations              — writer reports a batch_update result
  GET  /attestations/{node_id}    — list a Tree's attestations newest first
  GET  /attestations/{node_id}/latest — convenience for the dashboard
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, models, seasons
from ..db import get_db
from ..session_deps import require_writer
from ..uptime_calc import hours_online_for

router = APIRouter()
log = logging.getLogger("orchard.oracle.attest")


class AttestationRecord(BaseModel):
    node_id: str = Field(..., description="32 hex chars")
    season_number: int = Field(..., ge=1)
    hours_online: int = Field(..., ge=0, le=24)
    data_hash: str = Field(..., description="sha256 hex of the canonical bytes that were signed")
    oracle_sig: str = Field(
        ...,
        description=(
            "Oracle season signature hex — secp256r1 r||s (128 hex) for "
            "ADR-0003 records, or legacy HMAC-SHA256 (64 hex)"
        ),
    )
    # Nullable. A record recovered FROM the chain (sync-oracle) carries no
    # transaction id: a sealed record does not name the transaction that placed
    # it, and there is no reliable way to recover it afterwards. Sending null is
    # the honest option — a fabricated tx id would be worse than an absent one
    # because it would look checkable. The proof is data_hash + oracle_sig
    # against the record on chain, neither of which needs the tx id.
    dl_tx_id: str | None = Field(None, description="Chia DataLayer batch tx id (0x... or hex)")
    dl_key_hex: str = Field(..., description="hex of the `attest:<node>:<season>` key")
    block_height_at_write: int | None = None
    written_to_datalayer_at: datetime | None = None


class AttestationPublic(BaseModel):
    node_id: str
    season_number: int
    hours_online: int
    data_hash: str
    oracle_sig: str | None
    dl_tx_id: str | None
    dl_key_hex: str | None
    block_height_at_write: int | None
    written_to_datalayer_at: datetime | None
    created_at: datetime
    # Integrity cross-check populated on POST for a CLOSED season: the oracle's
    # own authoritative hours_online, and whether the writer's report matched.
    # None on GET (not recomputed) and for in-progress seasons (still changing).
    oracle_hours_online: int | None = None
    hours_match: bool | None = None


def _to_public(a: models.Attestation) -> AttestationPublic:
    return AttestationPublic(
        node_id=a.node_id,
        season_number=a.season_number,
        hours_online=a.hours_online,
        data_hash=a.data_hash,
        oracle_sig=a.oracle_sig,
        dl_tx_id=a.dl_tx_id,
        dl_key_hex=a.dl_key_hex,
        block_height_at_write=a.block_height_at_write,
        written_to_datalayer_at=a.written_to_datalayer_at,
        created_at=a.created_at,
    )


@router.post(
    "/attestations",
    response_model=AttestationPublic,
    status_code=status.HTTP_201_CREATED,
)
def record_attestation(
    rec: AttestationRecord,
    request: Request,
    db: Session = Depends(get_db),
    _writer: None = Depends(require_writer),
) -> AttestationPublic:
    """Writer reports a successful DataLayer batch_update.

    Idempotent on (node_id, season_number): re-running the writer
    against an already-recorded attestation updates the tx_id +
    timestamps instead of erroring. The writer's natural retry path
    therefore Just Works without poisoning the table with duplicates.

    Auth (2026-06-09 hardening): gated by ``require_writer`` — a writer
    token when configured, else loopback-only. This stops any LAN host
    from forging/overwriting the "on-chain" records the dashboard
    presents as verified.
    """
    # Normalize node_id casing — matches the convention used elsewhere
    # in the oracle so a lower-case writer call doesn't fragment rows.
    node_id = rec.node_id.upper()

    # Reject attestations for nodes we don't know about. The writer
    # only ever sees nodes via the oracle's /nodes endpoint anyway,
    # so this should never fire in practice — but it stops a bad
    # write from creating an orphan row.
    if db.get(models.Node, node_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown node_id: {node_id}",
        )

    written_at = rec.written_to_datalayer_at or datetime.now(timezone.utc)

    # Integrity cross-check: the oracle records this as "on chain" truth for the
    # dashboard, so validate the writer's reported hours_online against the
    # oracle's OWN authoritative uptime for the season. Only for a CLOSED season
    # (an in-progress season's count is still changing). A mismatch doesn't
    # reject the write (the value IS what's on chain) but is flagged in the
    # response and recorded as an audit event so silent divergence — a writer
    # bug or a token-compromised writer — can't pass unseen.
    oracle_hours: int | None = None
    hours_match: bool | None = None
    if rec.season_number < seasons.current_season():
        oracle_hours, _ = hours_online_for(db, node_id, rec.season_number)
        hours_match = rec.hours_online == oracle_hours
        if not hours_match:
            log.warning(
                "attestation hours mismatch node=%s season=%s reported=%s oracle=%s",
                node_id, rec.season_number, rec.hours_online, oracle_hours,
            )
            audit.record(
                db,
                action="attestation.hours_mismatch",
                node_id=node_id,
                actor="writer",
                request_id=audit.request_id_of(request),
                season=rec.season_number,
                reported_hours=rec.hours_online,
                oracle_hours=oracle_hours,
            )

    existing = db.execute(
        select(models.Attestation).where(
            models.Attestation.node_id == node_id,
            models.Attestation.season_number == rec.season_number,
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Re-write — update the chain pointers but preserve created_at.
        existing.hours_online = rec.hours_online
        existing.data_hash = rec.data_hash
        existing.oracle_sig = rec.oracle_sig
        existing.dl_tx_id = rec.dl_tx_id
        existing.dl_key_hex = rec.dl_key_hex
        existing.block_height_at_write = rec.block_height_at_write
        existing.written_to_datalayer_at = written_at
        db.commit()
        db.refresh(existing)
        out = _to_public(existing)
        out.oracle_hours_online = oracle_hours
        out.hours_match = hours_match
        return out

    a = models.Attestation(
        node_id=node_id,
        season_number=rec.season_number,
        hours_online=rec.hours_online,
        data_hash=rec.data_hash,
        oracle_sig=rec.oracle_sig,
        dl_tx_id=rec.dl_tx_id,
        dl_key_hex=rec.dl_key_hex,
        block_height_at_write=rec.block_height_at_write,
        written_to_datalayer_at=written_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    out = _to_public(a)
    out.oracle_hours_online = oracle_hours
    out.hours_match = hours_match
    return out


@router.get(
    "/attestations/{node_id}",
    response_model=list[AttestationPublic],
)
def list_attestations(
    node_id: str,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[AttestationPublic]:
    """Newest first, capped at ``limit``."""
    node_id = node_id.upper()
    if db.get(models.Node, node_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown node_id",
        )
    rows = db.execute(
        select(models.Attestation)
        .where(models.Attestation.node_id == node_id)
        .order_by(models.Attestation.season_number.desc())
        .limit(limit)
    ).scalars().all()
    return [_to_public(r) for r in rows]


@router.get(
    "/attestations/{node_id}/latest",
    response_model=AttestationPublic | None,
)
def latest_attestation(
    node_id: str,
    db: Session = Depends(get_db),
) -> AttestationPublic | None:
    """Just the most recent attestation, or null when there is none.
    Used by the dashboard's "On chain" card so it doesn't have to pull
    the full list every poll."""
    node_id = node_id.upper()
    if db.get(models.Node, node_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown node_id",
        )
    row = db.execute(
        select(models.Attestation)
        .where(models.Attestation.node_id == node_id)
        .order_by(models.Attestation.season_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _to_public(row) if row is not None else None
