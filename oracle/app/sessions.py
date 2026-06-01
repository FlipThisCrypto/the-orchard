# SPDX-License-Identifier: Apache-2.0
"""Session token issuance + validation (Phase 6.6).

A "session" represents a verified wallet — an xch1 address whose
holder has demonstrated control by signing a fresh challenge with
their wallet. Tokens are short-lived JWTs signed with
``settings().session_secret`` and carry just enough state to scope
oracle queries to that operator.

Token shape (HS256 JWT):

    {
      "sub": "xch1...",      # the verified address
      "iat": <int unix ts>,  # issued
      "exp": <int unix ts>,  # expires
      "jti": "<uuid4 hex>",  # unique per token (future revocation)
    }

We use HS256 because the same process that issues a token also
validates it — no separate verifier needs the public key. Switching
to RS256/EdDSA is a one-line change later if we want offline
verification.

Challenge nonces are separately tracked in-memory; they expire on
``settings().challenge_ttl_seconds`` and are single-use (consumed by
/auth/verify). Restarting the oracle wipes pending challenges, which
is fine: the worst case is the operator re-clicks Connect Wallet.
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass

import jwt

from .config import settings


# ---------- session token issue/validate ----------------------------

class SessionError(RuntimeError):
    """Raised on any token validation failure — expired, malformed,
    bad signature, missing claims. Callers translate to HTTP 401."""


@dataclass
class Session:
    address: str   # the verified xch1...
    expires_at: int
    jti: str


def _session_secret() -> bytes:
    """Return the HS256 signing key. Generated lazily on first call
    when the configured ``session_secret`` is empty so we never ship
    a known default. Operators with a fixed value (e.g. multi-process
    deployment) set ``ORCHARD_ORACLE_SESSION_SECRET=...`` in their
    .env."""
    s = settings().session_secret
    if not s:
        # Module-cached per process — restart rotates all sessions.
        s = _ephemeral_secret()
    return s.encode("utf-8") if isinstance(s, str) else s


_ephemeral_cache: bytes | None = None


def _ephemeral_secret() -> str:
    global _ephemeral_cache
    if _ephemeral_cache is None:
        _ephemeral_cache = secrets.token_hex(32).encode("ascii")
    return _ephemeral_cache.decode("ascii")


def issue(address: str) -> tuple[str, Session]:
    """Mint a session token for ``address``. Returns (token, session)."""
    ttl = max(60, int(settings().session_ttl_seconds))
    now = int(time.time())
    exp = now + ttl
    jti = uuid.uuid4().hex
    payload = {"sub": address, "iat": now, "exp": exp, "jti": jti}
    tok = jwt.encode(payload, _session_secret(), algorithm="HS256")
    return tok, Session(address=address, expires_at=exp, jti=jti)


def validate(token: str) -> Session:
    """Validate a session token. Returns the Session on success or
    raises SessionError. Caller maps to HTTP 401."""
    try:
        decoded = jwt.decode(
            token, _session_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise SessionError("session expired") from e
    except jwt.InvalidTokenError as e:
        raise SessionError(f"invalid session token: {e}") from e
    sub = decoded.get("sub")
    exp = decoded.get("exp")
    jti = decoded.get("jti")
    if not sub or not exp or not jti:
        raise SessionError("session token missing required claims")
    return Session(address=sub, expires_at=int(exp), jti=jti)


# ---------- challenge nonces ----------------------------------------

# In-memory store: nonce -> (issued_at_unix).
# Single uvicorn worker by default, so per-process is fine.
# Phase 11 hardening: Redis-backed if we ever scale out.

_nonces: dict[str, int] = {}
_nonces_lock = threading.Lock()


def issue_challenge() -> tuple[str, int]:
    """Mint a single-use nonce and return (nonce, expires_at_unix).

    The wallet signs this exact string; we look it up on
    /auth/verify, consume it, and refuse if it's not present or has
    expired.
    """
    ttl = max(30, int(settings().challenge_ttl_seconds))
    now = int(time.time())
    expires = now + ttl
    # 32-byte random nonce, hex-encoded. Long enough that guessing
    # within the TTL is intractable; short enough that the wallet's
    # signMessage prompt is readable.
    nonce = secrets.token_hex(32)
    with _nonces_lock:
        _gc_locked(now)
        _nonces[nonce] = expires
    return nonce, expires


def consume_challenge(nonce: str) -> bool:
    """Atomically check + remove a nonce. Returns True if the nonce
    existed and was unexpired (and is now consumed). False otherwise.
    Same-nonce replay is prevented because consume happens in the
    same critical section as the existence check.
    """
    now = int(time.time())
    with _nonces_lock:
        exp = _nonces.pop(nonce, None)
        if exp is None:
            return False
        if exp < now:
            return False
        return True


def _gc_locked(now: int) -> None:
    """Drop expired nonces. Must be called with _nonces_lock held."""
    stale = [n for n, e in _nonces.items() if e < now]
    for n in stale:
        _nonces.pop(n, None)


def reset_for_tests() -> None:
    """Drop both the secret cache and pending challenges. Used by the
    test client fixture so tests don't share state."""
    global _ephemeral_cache
    _ephemeral_cache = None
    with _nonces_lock:
        _nonces.clear()
