# SPDX-License-Identifier: Apache-2.0
"""Root health endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from .. import seasons
from ..observability import METRICS
from ..session_deps import LOOPBACK_HOSTS

router = APIRouter()


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
    return {"ok": True}


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
