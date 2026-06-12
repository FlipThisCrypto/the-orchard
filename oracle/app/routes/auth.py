# SPDX-License-Identifier: Apache-2.0
"""Wallet-auth routes (Phase 6.6).

The challenge-response handshake mirrors the WalletConnect / CHIP-22
flow used by Sage and Goby:

    1. Browser hits  POST /auth/challenge          -> { nonce, expires, message }
    2. Wallet signs the message via chia_signMessageByAddress
                                                     -> { publicKey, signature }
    3. Browser hits  POST /auth/verify             -> { session_token, expires_at }
    4. Subsequent requests carry  Authorization: Bearer <session_token>

The "message" the wallet signs is a short human-readable challenge
string. Sage/Goby wrap it as ``("Chia Signed Message", message_bytes)
.tree_hash()`` internally before BLS-signing; the oracle mirrors that
recipe on verify (see ``wallet_auth.signed_message_payload``).

/auth/verify reconstructs the message from the nonce server-side
rather than trusting the client's copy — that closes a class of
"client sent a different message than it asked the wallet to sign"
shenanigans.

The verify step validates both that the BLS signature is valid AND
that the public key derives to the claimed xch1 address — without
the second check, an attacker could submit their own signed nonce
under someone else's address and pass.

Sessions are HS256 JWTs (see sessions.py); revocation in v1 is "wait
for it to expire" + "restart the oracle to invalidate everything."
A token blocklist is a Phase 11 hardening.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import sessions, wallet_auth
from ..config import settings
from ..session_deps import client_is_loopback, require_session

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("orchard.oracle.auth")


# ---------- request / response shapes -------------------------------

class ChallengeResponse(BaseModel):
    nonce: str = Field(..., description="random hex string, the per-challenge salt")
    expires_at: int = Field(..., description="unix timestamp")
    message: str = Field(..., description=(
        "the exact human-readable string the wallet displays in its "
        "sign prompt. The wallet internally wraps this in the "
        "CHIP-22 cons-cell (b'Chia Signed Message' . message_bytes) "
        "before BLS-signing — see oracle.app.wallet_auth."
    ))


class VerifyRequest(BaseModel):
    address: str = Field(..., description="xch1... wallet address")
    public_key: str = Field(..., description="48-byte synthetic G1 pubkey, hex")
    signature: str = Field(..., description="96-byte AugScheme G2 signature, hex")
    nonce: str = Field(..., description="the nonce returned by /auth/challenge")


class VerifyResponse(BaseModel):
    session_token: str
    address: str
    expires_at: int


class WhoAmI(BaseModel):
    address: str
    expires_at: int


# ---------- routes ---------------------------------------------------
#
# (The session-dep helpers used to live here as local functions; they
# were duplicated in routes/nodes.py too. Both now import from
# oracle.app.session_deps. ``require_session`` is re-exported at the
# top of this file for the same import path the rest of the codebase
# was already using.)

def _message_for_nonce(nonce: str) -> str:
    """The exact UTF-8 string the wallet displays in its sign prompt.
    Reconstructed server-side from the nonce on both /challenge AND
    /verify so the client can't ask the wallet to sign one thing and
    submit "I signed something else" to us. Operators see this string
    in the Sage/Goby modal — keep it readable and self-describing."""
    return (
        "Sign in to Orchard View.\n\n"
        f"Challenge nonce: {nonce}\n"
        "If you didn't initiate this, decline."
    )


@router.post("/challenge", response_model=ChallengeResponse)
def challenge() -> ChallengeResponse:
    """Issue a one-time nonce for the wallet to sign."""
    nonce, expires_at = sessions.issue_challenge()
    return ChallengeResponse(
        nonce=nonce,
        expires_at=expires_at,
        message=_message_for_nonce(nonce),
    )


@router.post("/verify", response_model=VerifyResponse)
def verify(req: VerifyRequest, request: Request) -> VerifyResponse:
    """Verify a signed challenge and issue a session token."""
    # 1) consume the nonce (atomic with the existence check). Replay
    #    of the same nonce is impossible because the second consume()
    #    returns False even within the TTL window.
    if not sessions.consume_challenge(req.nonce):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="challenge nonce unknown or expired — request a fresh one",
        )

    # 2) verify the BLS signature AND that the pubkey binds to the
    #    claimed address. The message we verify against is rebuilt
    #    server-side from the nonce so a malicious client can't ask
    #    the wallet to sign "transfer 1000 XCH" and then submit that
    #    sig to log in. test_mode lets the test suite exercise the
    #    flow without a real wallet; production requires
    #    settings().auth_test_mode = False (the default).
    # test_mode (skip BLS) is honored ONLY for loopback callers, so a
    # mis-set ORCHARD_ORACLE_AUTH_TEST_MODE can't silently downgrade auth
    # for a remote client on a 0.0.0.0-bound oracle.
    effective_test_mode = settings().auth_test_mode and client_is_loopback(request)
    result = wallet_auth.verify_chia_signed_message(
        address=req.address,
        public_key_hex=req.public_key,
        signature_hex=req.signature,
        message=_message_for_nonce(req.nonce),
        test_mode=effective_test_mode,
    )
    if not result.ok:
        # Log the precise reason server-side; return a generic message so
        # the endpoint isn't a free verification oracle for an attacker.
        log.info("auth/verify failed for %s: %s", req.address, result.reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="signature verification failed",
        )

    token, sess = sessions.issue(req.address)
    return VerifyResponse(
        session_token=token,
        address=sess.address,
        expires_at=sess.expires_at,
    )


@router.get("/whoami", response_model=WhoAmI)
def whoami(sess: sessions.Session = Depends(require_session)) -> WhoAmI:
    """Echo back the current session's verified address + TTL. Useful
    for the dashboard to confirm it's still authenticated before
    rendering operator-only UI."""
    return WhoAmI(address=sess.address, expires_at=sess.expires_at)
