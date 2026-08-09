# SPDX-License-Identifier: Apache-2.0
"""FastAPI app entry point.

Run with:
    python -m oracle.app.main
or
    uvicorn oracle.app.main:app --host 0.0.0.0 --port 8000

Schema is created on startup via db.create_all(); versioned migrations
live in oracle/migrations (Alembic) — see that dir's README.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db, observability
from .config import settings
from .ratelimit import FixedWindowLimiter
from .routes import (
    attestations,
    auth,
    beacon,
    claim_page,
    health,
    network,
    nodes,
    provision,
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
    # Capture the applied migration revision once, here, AFTER create_all /
    # migrations. /health then serves it from memory and never touches the DB
    # during a request — liveness must survive a dead database.
    health.prime_schema_head()
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

# Cross-origin allowlist for the shared wallet widget: the landing page +
# dashboard (other Orchard subdomains) call /auth, /provision and /nodes
# cross-origin. Auth rides as a Bearer header (not a sent cookie), so
# allow_credentials stays False and origins are an explicit allowlist plus a
# *.theorchard.network regex (never "*"). The same-origin claim page is
# unaffected.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origin_list(),
    allow_origin_regex=settings().cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(claim_page.router)
app.include_router(register.router)
app.include_router(provision.router)
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


# --- Request body-size guard (before observability so 413s are logged) -----
# Reject an oversized body early via Content-Length so a single request can't
# force the server to buffer an unbounded payload. Covers every POST/PUT path.
@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    max_bytes = settings().max_request_body_bytes
    if max_bytes > 0:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "request body too large"},
                    )
            except ValueError:
                pass  # malformed header — let the normal parse path reject it
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
    # Deploy markers on EVERY response, so "did my deploy land?" is one curl
    # against any endpoint the edge allows — instead of an SSH session and two
    # greps, which is how it was answered twice on 2026-08-08, once wrongly.
    #
    # Headers rather than a body field: /health's body is a contract an
    # existing test pins ({"ok": true} exactly), `/` and `/health/ready` are
    # 403'd to script clients, and a new path would be a bet on opaque edge
    # rules. A header sidesteps all three.
    response.headers["X-Orchard-Source"] = health.SOURCE_FINGERPRINT
    response.headers["X-Orchard-Schema"] = health.schema_head()
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
