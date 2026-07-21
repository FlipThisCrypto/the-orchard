# SPDX-License-Identifier: Apache-2.0
"""Root health endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from .. import seasons

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
