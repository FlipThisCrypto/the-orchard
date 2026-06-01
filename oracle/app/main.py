# SPDX-License-Identifier: Apache-2.0
"""FastAPI app entry point.

Run with:
    python -m oracle.app.main
or
    uvicorn oracle.app.main:app --host 0.0.0.0 --port 8000

Schema is created on startup; for migrations later we'll add Alembic.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .config import settings
from .routes import attestations, auth, health, nodes, readings, register, uptime


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
