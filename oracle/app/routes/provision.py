# SPDX-License-Identifier: Apache-2.0
"""Remote claim-code provisioning (ADR-0005, HANDOVER T9).

Three steps, three endpoints:

  POST /provision/announce   (Tree, unauthenticated)
      First boot: the Tree registers itself UNPROVISIONED (its node_id +
      HMAC signing key, no wallet) and the human-readable claim code it shows
      over serial. Same trust model as /register's key submission.

  POST /provision/claim      (operator browser, wallet session)
      The operator submits the claim code; the oracle verifies their Orchard
      Pass and binds the Tree to their wallet. Single-use + expiring.

  GET  /provision/{code}     (Tree, polling)
      The Tree polls until the claim is consumed, then begins normal posting.

Wallet + Pass resolution is shared with /register (imported, not duplicated):
a session is the source of truth for the binding wallet, and a Pass is
required at claim time (ADR-0005 resolved decision #3).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models, sessions
from ..db import get_db
from ..session_deps import maybe_session
from .register import (
    _HEX32,
    _HEX64,
    _resolve_pass_nft_id,
    _resolve_wallet_for_register,
)

router = APIRouter(prefix="/provision", tags=["provision"])

# Claim codes live 24h (ADR-0005). Short enough to limit a stolen code's
# window, long enough that an operator can flash now and claim later.
CLAIM_TTL = timedelta(hours=24)

# A normalized code: dashes/spaces stripped, upper-cased. We validate shape +
# case, not the exact alphabet (the firmware picks Crockford-base32).
_CLAIM_RE = re.compile(r"^[0-9A-Z]{6,12}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes even for DateTime(timezone=True); treat
    # a stored value as UTC so it compares cleanly against _now().
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _normalize_code(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw or "").upper()


class AnnounceRequest(BaseModel):
    node_id: str = Field(..., description="32 hex chars (16 bytes)")
    signing_key_hex: str = Field(..., description="64 hex chars (32-byte HMAC secret)")
    claim_code: str = Field(..., description="human-readable code the Tree shows the operator")
    label: str | None = None
    fw_version: str | None = None

    @field_validator("node_id")
    @classmethod
    def _nid(cls, v: str) -> str:
        if not _HEX32.match(v):
            raise ValueError("node_id must be 32 hex characters")
        return v.upper()

    @field_validator("signing_key_hex")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("signing_key_hex must be 64 hex characters")
        key = v.upper()
        if int(key, 16) == 0:
            raise ValueError("signing_key_hex must not be all zeros")
        return key


class ClaimRequest(BaseModel):
    claim_code: str
    # Legacy/testing path only — ignored when a wallet session is present, and
    # rejected by _resolve_wallet_for_register when require_wallet_session=true.
    wallet_address: str | None = None


@router.post("/announce", status_code=status.HTTP_201_CREATED)
def announce(req: AnnounceRequest, db: Session = Depends(get_db)) -> dict:
    code = _normalize_code(req.claim_code)
    if not _CLAIM_RE.match(code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="claim_code must be 6-12 alphanumeric chars")

    node = db.get(models.Node, req.node_id)
    if node is None:
        node = models.Node(node_id=req.node_id, signing_key_hex=req.signing_key_hex,
                           label=req.label, fw_version=req.fw_version, registered_at=_now())
        db.add(node)
    else:
        # A different key on the same node_id means a different physical Tree
        # claiming the identity — refuse (same rule as /register).
        if node.signing_key_hex.upper() != req.signing_key_hex.upper():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="node_id already known with a different signing key")
        if node.wallet_address is not None:
            # Already claimed/registered — tell the Tree so it stops waiting.
            db.commit()
            return {"claimed": True, "node_id": node.node_id}
        if req.label is not None:
            node.label = req.label
        if req.fw_version is not None:
            node.fw_version = req.fw_version

    claim = db.get(models.Claim, code)
    if claim is None:
        db.add(models.Claim(claim_code=code, node_id=req.node_id,
                            created_at=_now(), expires_at=_now() + CLAIM_TTL))
    elif claim.node_id != req.node_id:
        # Code already maps to a different node (astronomically unlikely with a
        # good derivation) — refuse rather than silently re-point it.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="claim_code already in use by another node")
    elif claim.consumed_at is None:
        claim.expires_at = _now() + CLAIM_TTL  # refresh the window on re-announce

    db.commit()
    return {"claimed": False, "claim_code": code}


@router.post("/claim")
def claim(
    req: ClaimRequest,
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(maybe_session),
) -> dict:
    code = _normalize_code(req.claim_code)
    # Wallet resolution + auth policy, shared with /register (401 if no session
    # when require_wallet_session; body wallet only on the legacy path).
    wallet = _resolve_wallet_for_register(req.wallet_address, sess)
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="claiming a Tree requires a wallet")

    claim_row = db.get(models.Claim, code)
    if claim_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown claim code")
    if claim_row.consumed_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="claim already used")
    if _aware(claim_row.expires_at) < _now():
        raise HTTPException(status_code=status.HTTP_410_GONE,
                            detail="claim code expired — re-announce from the Tree")

    # Pass required at claim time (ADR-0005 #3); 403 if the wallet holds none.
    pass_nft_id, pass_verified_at = _resolve_pass_nft_id(wallet)

    # Single-use: guarded consume so two simultaneous claims can't both bind.
    consumed = db.execute(
        update(models.Claim)
        .where(models.Claim.claim_code == code, models.Claim.consumed_at.is_(None))
        .values(consumed_at=_now(), claimed_by=wallet)
    ).rowcount
    if not consumed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="claim already used")

    node = db.get(models.Node, claim_row.node_id)
    if node is not None:
        node.wallet_address = wallet
        node.pass_nft_id = pass_nft_id
        node.pass_verified_at = pass_verified_at
    db.commit()
    return {"claimed": True, "node_id": claim_row.node_id, "pass_nft_id": pass_nft_id}


@router.get("/{claim_code}")
def poll(claim_code: str, db: Session = Depends(get_db)) -> dict:
    code = _normalize_code(claim_code)
    claim_row = db.get(models.Claim, code)
    if claim_row is None:
        return {"claimed": False, "known": False}
    claimed = claim_row.consumed_at is not None
    # `label` lets the claim page preview WHICH Tree a code maps to before the
    # operator binds it (a known unclaimed code only reveals a human label; the
    # code space is large enough that enumeration isn't a concern). node_id is
    # still only returned once claimed, matching prior behavior.
    node = db.get(models.Node, claim_row.node_id)
    return {"claimed": claimed, "known": True,
            "label": node.label if node else None,
            "node_id": claim_row.node_id if claimed else None}
