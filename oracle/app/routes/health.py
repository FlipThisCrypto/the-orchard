# SPDX-License-Identifier: Apache-2.0
"""Root health endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import text

from .. import db, seasons
from ..observability import METRICS
from ..session_deps import LOOPBACK_HOSTS

router = APIRouter()


def _check_db() -> tuple[bool, str]:
    """Cheap DB connectivity probe (``SELECT 1``). Returns (ok, detail)."""
    try:
        with db.engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as e:  # noqa: BLE001 — any failure means not-ready
        return False, f"error: {type(e).__name__}"


@router.get("/")
def root() -> dict:
    """Cheap liveness + self-identification."""
    return {
        "service": "the-orchard-oracle",
        "version": "0.1.0",
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "current_season": seasons.current_season(),
        # Orchard DataLayer publish schema (docs/datalayer/SPEC.md).
        "datalayer_schema": "1.0.0",
    }


@router.get("/health")
def health() -> dict:
    """Liveness: the process is up and serving. Cheap, no dependencies —
    suitable for a load balancer's frequent poll."""
    return {"ok": True}


@router.get("/health/ready")
def readiness(response: Response) -> dict:
    """Readiness: the service can actually serve (its DB is reachable).

    Returns 200 when every dependency check passes, else **503** so an
    orchestrator/monitor can pull a broken-but-running instance out of
    rotation (a DB that is down, locked, or file-permission-broken).
    """
    db_ok, db_detail = _check_db()
    checks = {"db": db_detail}
    ready = db_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": ready,
        "checks": checks,
        "now_utc": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/metrics")
def metrics(request: Request) -> dict:
    """In-process request metrics (totals, error count, per-route latency).

    Loopback-only: this exposes traffic patterns, so remote callers get 403.
    The operator's monitoring runs on the oracle host (localhost-bound by
    default). See ``observability.Metrics``.
    """
    host = request.client.host if request.client else None
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="metrics are loopback-only",
        )
    return METRICS.snapshot()
