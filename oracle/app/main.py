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

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .ratelimit import FixedWindowLimiter
from .routes import attestations, auth, health, network, nodes, readings, register, uptime
from .session_deps import LOOPBACK_HOSTS


@asynccontextmanager
async def _lifespan(app: FastAPI):
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
