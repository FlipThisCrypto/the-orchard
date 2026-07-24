# SPDX-License-Identifier: Apache-2.0
"""FastAPI app entry point.

Run with:
    python -m oracle.app.main
or
    uvicorn oracle.app.main:app --host 0.0.0.0 --port 8000

Schema is created on startup; for migrations later we'll add Alembic.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, observability
from .config import settings
from .ratelimit import FixedWindowLimiter
from .routes import (
    attestations,
    auth,
    beacon,
    health,
    network,
    nodes,
    readings,
    register,
    uptime,
)
from .session_deps import LOOPBACK_HOSTS


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Emit the observability logs at the configured level (they propagate to
    # uvicorn's root handlers). Without this the request/error lines default to
    # WARNING and INFO request logs would be dropped.
    observability.logger.setLevel(settings().log_level.upper())
    db.create_all()
    yield


app = FastAPI(
    title="The Orchard — Oracle",
    description=(
        "Receives signed sensor readings from Tree firmware, stores them in "
        "SQLite, and exposes per-Tree readings + Season uptime queries. "
        "Part of The Orchard — an open-source environmental DePIN on Chia."
    ),
    version="0.1.0",
    lifespan=_lifespan,
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(register.router)
app.include_router(readings.router)
app.include_router(nodes.router)
app.include_router(uptime.router)
app.include_router(attestations.router)
app.include_router(network.router)
app.include_router(beacon.router)


# --- Rate limiting (2026-06-09 hardening) ---------------------------------
# Bound remote LAN callers on the unauthenticated/sensitive endpoints.
# Loopback (the operator's own dashboard + local writer, and the in-process
# test client) is exempt, so this never throttles normal local use.
_limiters: dict[str, FixedWindowLimiter] = {}


def _limiter_for(name: str, limit: int) -> FixedWindowLimiter:
    lm = _limiters.get(name)
    if lm is None or lm.limit != limit:
        lm = FixedWindowLimiter(limit)
        _limiters[name] = lm
    return lm


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    host = request.client.host if request.client else None
    if host not in LOOPBACK_HOSTS:
        s = settings()
        path = request.url.path
        rule: tuple[str, int] | None = None
        if path.startswith("/auth/"):
            rule = ("auth", s.auth_rate_limit_per_min)
        elif path.startswith("/readings"):
            rule = ("readings", s.readings_rate_limit_per_min)
        if rule is not None and not _limiter_for(*rule).allow(host or "?"):
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded; slow down"},
            )
    return await call_next(request)


# --- Observability (outermost: wraps the rate limiter + all routes) --------
# Defined AFTER _rate_limit so Starlette runs it first/outermost — it times and
# logs every response (including 429s), captures unhandled exceptions as a clean
# JSON 500, and stamps a correlation id on every response.
@app.middleware("http")
async def _observe(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or observability.new_request_id()
    request.state.request_id = rid
    label = observability.route_label(request.method, request.url.path)
    start = time.perf_counter()
    client = request.client.host if request.client else "?"
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 — last-resort capture for operator signal
        dur = (time.perf_counter() - start) * 1000.0
        observability.METRICS.record(label, 500, dur)
        observability.logger.exception(
            "request_error rid=%s %s dur_ms=%.1f client=%s", rid, label, dur, client
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal server error", "request_id": rid},
            headers={"X-Request-ID": rid},
        )
    dur = (time.perf_counter() - start) * 1000.0
    observability.METRICS.record(label, response.status_code, dur)
    observability.logger.info(
        "request rid=%s %s status=%s dur_ms=%.1f client=%s",
        rid, label, response.status_code, dur, client,
    )
    response.headers["X-Request-ID"] = rid
    return response


def main() -> None:
    """Entry point for `python -m oracle.app.main`."""
    import uvicorn
    s = settings()
    uvicorn.run(
        "oracle.app.main:app",
        host=s.host,
        port=s.port,
        log_level=s.log_level,
        reload=False,
    )


if __name__ == "__main__":
    main()
