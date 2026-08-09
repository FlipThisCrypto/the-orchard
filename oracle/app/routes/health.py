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


def _datalayer_schema_version() -> str:
    """The DataLayer publish-schema version this deployment writes/reads.

    Sourced from orchard_chia so it cannot drift from the real schema. Falls
    back to "unknown" when orchard_chia isn't importable (oracle-only install).
    """
    try:
        from orchard_chia.datalayer import SCHEMA_VERSION
        return SCHEMA_VERSION
    except Exception:  # noqa: BLE001 — advertising a version is best-effort
        return "unknown"


def _source_fingerprint() -> str:
    """Short hash of the oracle source files actually running.

    Answers "did my deploy land?" from a single public curl, instead of an SSH
    session and two greps — which is how it was answered twice on 2026-08-08,
    once incorrectly (a deploy that omitted orchard_chia/ looked successful
    because nothing could show what was running).

    Deliberately NOT ``git rev-parse HEAD``. The deploy is
    ``git checkout origin/main -- oracle/``, which updates FILES without moving
    HEAD, so HEAD would keep reporting the previous commit while new code ran.
    A version marker that lies is worse than none — the whole point is to be
    able to trust it.

    Hashing the bytes on disk cannot lie: recompute it from a checkout and
    compare. Computed once at import, so /health stays cheap enough for a load
    balancer to poll.
    """
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]        # oracle/app
    h = hashlib.sha256()
    try:
        for p in sorted(root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    except OSError:
        return "unknown"
    return h.hexdigest()[:12]


_SOURCE_FINGERPRINT = _source_fingerprint()


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
        # Orchard DataLayer publish schema (docs/datalayer/SPEC.md). Read from
        # the package rather than hardcoded — a literal here silently drifted
        # from the real schema version once already.
        "datalayer_schema": _datalayer_schema_version(),
    }


@router.get("/health")
def health() -> dict:
    """Liveness: the process is up and serving. Cheap, no dependencies —
    suitable for a load balancer's frequent poll.

    Carries the deploy markers because this is the endpoint that survives the
    edge: `/` and `/health/ready` are answered with a 403 challenge to script
    clients, so they cannot be used to check a deployment from outside.
    """
    return {
        "ok": True,
        "source": _SOURCE_FINGERPRINT,          # hash of the .py files running
        "datalayer_schema": _datalayer_schema_version(),
        "schema_head": _alembic_head(),          # applied DB migration
    }


_SCHEMA_HEAD: str | None = None


def _alembic_head() -> str:
    """The migration revision the live database is actually at.

    Distinct from the source fingerprint on purpose: code and schema deploy
    together but can land apart — a checkout without a restart leaves new code
    unloaded, and a restart without a migration leaves the schema behind. One
    field cannot express both, and conflating them is how "it deployed" gets
    believed when only half of it did.

    Read ONCE and cached, for two reasons. /health is liveness — "cheap, no
    dependencies", polled by a load balancer — and querying the database on
    every poll would quietly turn it into a readiness check, which is what
    /health/ready already is. And migrations run at startup, so the head cannot
    change while the process lives; a per-request query would re-read a constant.

    Computed lazily rather than at import because at import time the startup
    migration has not run yet, and the honest answer then is not the answer a
    caller wants.
    """
    global _SCHEMA_HEAD
    if _SCHEMA_HEAD is not None:
        return _SCHEMA_HEAD
    try:
        with db.engine().connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
        head = str(row[0]) if row else "none"
    except Exception:  # noqa: BLE001 — a health probe must not fail on this
        return "unknown"     # not cached: a transient DB blip must not stick
    _SCHEMA_HEAD = head
    return head


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
