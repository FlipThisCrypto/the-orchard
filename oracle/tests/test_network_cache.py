# SPDX-License-Identifier: Apache-2.0
"""Single-flight cache for the public /network/stats endpoint."""
from __future__ import annotations

import os

os.environ.setdefault("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")
# NOTE: the cache TTL is set PER-TEST via the fixture's monkeypatch.setenv (not
# at module import) so it never leaks into other test modules' env.

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from oracle.app.config import reset_settings_for_tests  # noqa: E402
from oracle.app.db import Base, get_db, reset_for_tests  # noqa: E402
from oracle.app.main import app  # noqa: E402
from oracle.app.routes import network  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    monkeypatch.setenv("ORCHARD_NETWORK_STATS_TTL_S", "300")
    reset_settings_for_tests()
    reset_for_tests()
    network.reset_cache_for_tests()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine, autoflush=False, future=True)

    def _override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()
    network.reset_cache_for_tests()


def _register(client, nid):
    return client.post("/register", json={"node_id": nid, "signing_key_hex": "11" * 32})


def test_stats_are_cached_within_ttl(client):
    r1 = client.get("/network/stats").json()
    assert r1["trees_registered"] == 0
    # Add a tree, but the cached snapshot should still be served (TTL not up).
    _register(client, "AABBCCDDEEFF00112233445566778899")
    r2 = client.get("/network/stats").json()
    assert r2["trees_registered"] == 0  # served from cache
    assert r2["as_of_utc"] == r1["as_of_utc"]  # same snapshot instant


def test_reset_cache_recomputes(client):
    client.get("/network/stats")
    _register(client, "AABBCCDDEEFF00112233445566778899")
    network.reset_cache_for_tests()
    r = client.get("/network/stats").json()
    assert r["trees_registered"] == 1


def test_ttl_zero_disables_cache(client, monkeypatch):
    monkeypatch.setenv("ORCHARD_NETWORK_STATS_TTL_S", "0")
    network.reset_cache_for_tests()
    client.get("/network/stats")
    _register(client, "AABBCCDDEEFF00112233445566778899")
    # No caching -> the very next call reflects the new tree.
    assert client.get("/network/stats").json()["trees_registered"] == 1
