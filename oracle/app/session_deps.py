# SPDX-License-Identifier: Apache-2.0
"""FastAPI dependencies for session-bound routes (Phase 6.6).

Two flavors:

  - ``require_session``  — strict; 401 if no valid Bearer token.
  - ``maybe_session``    — optional; returns ``None`` for unauthenticated
                           requests, raises 401 only on a *malformed*
                           token (silent degradation would hide bugs).

These deps used to live duplicated across ``routes/auth.py`` and
``routes/nodes.py``. Centralizing them here means /register, /nodes,
/auth, and future routes all see the same auth semantics — there's no
"oh the auth check is slightly different in register" foot-gun.
"""
from __future__ import annotations

from fastapi import Header, HTTPException, status

from . import sessions


def _parse_bearer(authorization: str | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header.
    Returns None if the header is absent or doesn't use the Bearer
    scheme. Whitespace-tolerant; lowercases the scheme for comparison.
    """
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(None, 1)[1].strip()


def require_session(
    authorization: str | None = Header(None),
) -> sessions.Session:
    """Strict variant — mutations and identity-scoped routes use this.

    Raises 401 if the Authorization header is missing, malformed, or
    the token fails validation (expired, bad signature, missing
    claims).
    """
    token = _parse_bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return sessions.validate(token)
    except sessions.SessionError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def maybe_session(
    authorization: str | None = Header(None),
) -> sessions.Session | None:
    """Optional variant — read routes that scope per-operator when
    authed but stay public-readable when not.

    Returns ``None`` for an absent header. Raises 401 only when a
    token IS presented but fails to validate: silently degrading
    "your broken token" to "anonymous" would mask the client bug.
    """
    token = _parse_bearer(authorization)
    if token is None:
        return None
    try:
        return sessions.validate(token)
    except sessions.SessionError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )
