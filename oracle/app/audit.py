# SPDX-License-Identifier: Apache-2.0
"""Audit trail for consequential actions.

One coherent helper so every mutating endpoint records who did what, when, and
under which request — for accountability and incident investigation. The row is
added to the caller's session and committed atomically with the action it
describes (so a rolled-back action leaves no false audit record).

Never records secrets (signing keys, session tokens); ``detail`` should carry
only booleans, ids, and counts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy.orm import Session

from . import models, sessions


def actor_for(sess: "sessions.Session | None", *, fallback: str = "anonymous") -> str:
    """Human-meaningful actor label: the verified wallet address, or fallback."""
    if sess is not None and getattr(sess, "address", None):
        return sess.address
    return fallback


def request_id_of(request: Request | None) -> str | None:
    """The correlation id stamped by the observability middleware, if present."""
    if request is None:
        return None
    return getattr(request.state, "request_id", None)


def record(
    db: Session,
    *,
    action: str,
    node_id: str | None = None,
    actor: str = "unknown",
    request_id: str | None = None,
    **detail: object,
) -> models.AuditEvent:
    """Add an audit row to ``db`` (caller commits it with the action)."""
    ev = models.AuditEvent(
        ts=datetime.now(timezone.utc),
        action=action,
        node_id=node_id,
        actor=actor,
        request_id=request_id,
        detail_json=json.dumps(detail, sort_keys=True, default=str) if detail else None,
    )
    db.add(ev)
    return ev
