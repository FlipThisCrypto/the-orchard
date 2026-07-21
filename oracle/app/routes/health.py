# SPDX-License-Identifier: Apache-2.0
"""Root health endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response

from .. import db, metrics, seasons
from ..config import settings

router = APIRouter()


def _public_flags() -> dict:
    """Non-secret operator-visible posture flags for preflight / deploy checks.

    Never include tokens, session secrets, DB URLs, or signing material.
    """
    s = settings()
    return {
        "require_wallet_session": s.require_wallet_session,
        "require_seq": s.require_seq,
        "max_reading_age_seconds": s.max_reading_age_seconds,
        "max_reading_future_seconds": s.max_reading_future_seconds,
        "max_reading_body_bytes": s.max_reading_body_bytes,
        "auth_rate_limit_per_min": s.auth_rate_limit_per_min,
        "readings_rate_limit_per_min": s.readings_rate_limit_per_min,
        "provision_rate_limit_per_min": s.provision_rate_limit_per_min,
        "register_rate_limit_per_min": s.register_rate_limit_per_min,
    }


@router.get("/")
def root() -> dict:
    """Cheap liveness + self-identification."""
    return {
        "service": "the-orchard-oracle",
        "version": "0.1.0",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "current_season": seasons.current_season(),
        "flags": _public_flags(),
    }


@router.get("/health")
def health(response: Response) -> dict:
    """Readiness: process up **and** database answers ``SELECT 1``.

    Uptime robots / Cloudflare / preflight should treat non-200 or
    ``ok: false`` as down. On DB failure we return HTTP 503 so monitors
    that only look at status codes also fail closed.
    """
    db_ok, db_detail = db.ping()
    body = {
        "ok": db_ok,
        "db": db_detail,
        "flags": _public_flags(),
        # Counters only — no payloads, node_ids, or secrets (metrics.py).
        "metrics": metrics.as_public_dict(),
    }
    if not db_ok:
        response.status_code = 503
    return body
