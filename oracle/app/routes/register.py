# SPDX-License-Identifier: Apache-2.0
"""Tree registration.

A new Tree gets its node_id and HMAC signing secret on first boot
(see firmware/src/identity.cpp). The Orchard View dashboard reads both
from the Tree over USB-serial and POSTs them here so the oracle knows
how to verify future signed submissions.

Phase 6.5: when the registration request includes a wallet_address,
the oracle verifies on chain (via the MintGarden indexer) that the
wallet holds at least one Orchard Pass. If not, registration is
rejected with 403. The verified Pass NFT id gets bound to the Tree
record so downstream consumers (Phase 7 payout, dashboard credentials
display) can resolve which Pass authorized the Tree's existence.

Phase 6.6 hardening: the wallet address is no longer trusted from the
request body. The operator must first prove control of their wallet
via the WalletConnect challenge flow (see /auth/challenge + /auth/verify),
present the resulting session token here as ``Authorization: Bearer
<token>``, and the oracle uses ``session.address`` as the source of
truth for the Pass check and Tree binding. If the body still carries
a ``wallet_address`` field, it must match the session address — a
mismatch is a 400 (suggests a confused client). The body field is
kept on the type only for the brief transition period; once the
wizard is fully migrated everywhere it can be removed.

When ``settings().require_wallet_session`` is True (default), an
unauthenticated /register call is rejected with 401. Operators
running legacy curl/CI scripts can flip the setting to False during
transition, but the new wizard always sends a session.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import models, pass_verify, sessions
from ..config import settings
from ..db import get_db
from ..session_deps import maybe_session

router = APIRouter()
log = logging.getLogger("orchard.oracle.register")

_HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")  # 16 bytes / node_id
_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")  # 32 bytes / signing key
# bech32m XCH address: hrp "xch1" + base32 payload. Don't try to
# fully validate bech32 here — that's the wallet's job. Just sanity-
# check the prefix and a reasonable length so a typo doesn't get fed
# to the indexer URL.
_XCH_ADDR = re.compile(r"^xch1[0-9a-z]{50,80}$")


class RegisterRequest(BaseModel):
    node_id: str = Field(..., description="32 hex chars (16 bytes)")
    signing_key_hex: str = Field(..., description="64 hex chars (32 bytes HMAC secret)")
    wallet_address: str | None = Field(
        None,
        description=(
            "Optional Chia wallet address (xch1...). When provided, the "
            "oracle verifies on chain that this wallet holds at least one "
            "Orchard Pass and binds the first one to the Tree."
        ),
    )
    label: str | None = Field(None, description="Optional human-readable label")
    fw_version: str | None = None

    @field_validator("node_id")
    @classmethod
    def _node_id_hex(cls, v: str) -> str:
        if not _HEX32.match(v):
            raise ValueError("node_id must be 32 hex characters")
        return v.upper()

    @field_validator("signing_key_hex")
    @classmethod
    def _key_hex(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("signing_key_hex must be 64 hex characters")
        return v.upper()

    @field_validator("wallet_address")
    @classmethod
    def _wallet_addr(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        # Tolerate whitespace from a copy/paste. Don't lower-case —
        # the indexer expects the exact bech32m form.
        s = v.strip()
        if not _XCH_ADDR.match(s):
            raise ValueError(
                "wallet_address must be a Chia mainnet address starting with "
                "'xch1' followed by 50-80 lowercase base32 chars"
            )
        return s


class RegisterResponse(BaseModel):
    node_id: str
    registered_at: datetime
    new: bool
    # When a wallet was provided AND a Pass was verified on chain, the
    # bound NFT id (bech32 nft1...) and the verification timestamp.
    # Both null in legacy unverified registrations.
    pass_nft_id: str | None = None
    pass_verified_at: datetime | None = None


def _resolve_pass_nft_id(wallet_address: str | None) -> tuple[str | None, datetime | None]:
    """Verify on chain that ``wallet_address`` holds a Pass and return
    the first Pass's nft_id + the verification time. Returns
    (None, None) when ``wallet_address`` is None.

    Raises HTTPException(403) when the wallet holds no Pass.
    Raises HTTPException(503) when the indexer call itself failed —
    we'd rather make the operator retry than register without proof
    when proof was requested.
    """
    if not wallet_address:
        return None, None
    try:
        nft_id = pass_verify.first_pass_nft_id(wallet_address)
    except pass_verify.PassVerifyError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not verify Orchard Pass: indexer error: {e}",
        ) from e
    if nft_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "wallet does not hold an Orchard Pass — registration with "
                "a wallet_address requires Pass ownership. Omit "
                "wallet_address to register without a binding, or buy a "
                "Pass at https://mintgarden.io/collections/"
                "col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29"
            ),
        )
    return nft_id, pass_verify.utcnow()


def _resolve_wallet_for_register(
    req_wallet_address: str | None,
    sess: sessions.Session | None,
) -> str | None:
    """Reconcile the wallet identity for /register.

    Policy (Phase 6.6):
      - With a session: session.address is the source of truth. If the
        body also carries a wallet_address that doesn't match the
        session, return 400 — that's a confused or malicious client,
        better surfaced than silently overridden.
      - Without a session: behavior is controlled by the
        ``require_wallet_session`` setting. When True (default), reject
        with 401. When False, fall back to body.wallet_address and log
        a deprecation warning so operators know the legacy path is on
        its way out.

    Returns the wallet address to use for Pass check + Tree binding,
    or None if no wallet binding is desired (only possible on the
    legacy path with a missing body field).
    """
    if sess is not None:
        if req_wallet_address and req_wallet_address != sess.address:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "wallet_address in request body does not match the "
                    "authenticated session's verified address — drop the "
                    "body field (the session is the source of truth) or "
                    "reconnect the wallet you actually want to bind."
                ),
            )
        return sess.address

    if settings().require_wallet_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "/register requires a verified wallet session — call "
                "/auth/challenge, sign with your wallet via WalletConnect, "
                "POST /auth/verify, and resend with Authorization: Bearer "
                "<session_token>."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Legacy unauthenticated path. Still accepted (for now) so existing
    # curl/CI scripts keep working — but we want visibility into who's
    # still using it so we know when it's safe to retire.
    log.warning(
        "register: legacy unauthenticated registration (wallet=%s) — "
        "require_wallet_session is False; update your script to obtain "
        "a session token first",
        req_wallet_address or "<none>",
    )
    return req_wallet_address


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
def register(
    req: RegisterRequest,
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(maybe_session),
) -> RegisterResponse:
    # Resolve the wallet first — this is where the auth policy lives.
    # If it rejects (401, 400), we bail before touching the DB.
    wallet_address = _resolve_wallet_for_register(req.wallet_address, sess)

    # Resolve Pass binding BEFORE touching the DB so a verification
    # failure never leaves a half-registered row behind.
    pass_nft_id, pass_verified_at = _resolve_pass_nft_id(wallet_address)

    existing = db.get(models.Node, req.node_id)
    if existing is not None:
        # Re-registration is allowed but only with the same signing key — a
        # different key on the same node_id would mean a different physical
        # Tree claiming the same identity. Refuse.
        if existing.signing_key_hex.upper() != req.signing_key_hex.upper():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="node_id already registered with a different signing key",
            )
        # Update mutable metadata if provided. With a session, the
        # resolved wallet_address is always set (== sess.address) so
        # any re-registration through the wizard re-binds the wallet
        # to the connected one. That's correct: the operator's
        # intent in clicking Provision again is "this Tree is mine
        # now under the wallet I'm currently connected with."
        if wallet_address is not None:
            existing.wallet_address = wallet_address
            # When wallet changes, re-bind the Pass (or clear if the
            # operator deliberately moved to an unverified state by
            # passing a wallet that we already verified is empty —
            # which we'd have caught above before reaching here).
            existing.pass_nft_id = pass_nft_id
            existing.pass_verified_at = pass_verified_at
        if req.label is not None:
            existing.label = req.label
        if req.fw_version is not None:
            existing.fw_version = req.fw_version
        db.commit()
        db.refresh(existing)
        return RegisterResponse(
            node_id=existing.node_id,
            registered_at=existing.registered_at,
            new=False,
            pass_nft_id=existing.pass_nft_id,
            pass_verified_at=existing.pass_verified_at,
        )

    node = models.Node(
        node_id=req.node_id,
        signing_key_hex=req.signing_key_hex,
        wallet_address=wallet_address,
        label=req.label,
        fw_version=req.fw_version,
        pass_nft_id=pass_nft_id,
        pass_verified_at=pass_verified_at,
        registered_at=datetime.now(timezone.utc),
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return RegisterResponse(
        node_id=node.node_id,
        registered_at=node.registered_at,
        new=True,
        pass_nft_id=node.pass_nft_id,
        pass_verified_at=node.pass_verified_at,
    )
