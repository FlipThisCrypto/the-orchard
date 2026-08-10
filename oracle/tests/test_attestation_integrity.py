# SPDX-License-Identifier: Apache-2.0
"""Attestation hours_online cross-check against the oracle's own uptime."""
from __future__ import annotations

import os
os.environ.setdefault("ORCHARD_ORACLE_MIN_READINGS_PER_CREDITED_HOUR", "1")  # mechanism tests; quorum pinned in test_uptime_quorum.py

os.environ.setdefault("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from oracle.app import models, seasons  # noqa: E402
from oracle.app.config import reset_settings_for_tests  # noqa: E402
from oracle.app.db import Base, get_db, reset_for_tests  # noqa: E402
from oracle.app.main import app  # noqa: E402

NODE_ID = "0123456789ABCDEF0123456789ABCDEF"
KEY_HEX = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF"
CLOSED_SEASON = 3  # genesis + 2 days; far in the past relative to "now"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    reset_settings_for_tests()
    reset_for_tests()
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


def _seed_uptime(client, node_id, season, n):
    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    for b in seasons.hour_buckets_in_season(season)[:n]:
        db.add(models.UptimeHour(node_id=node_id, hour_utc=b, reading_count=1))
    db.commit()


def _attest_body(season, hours):
    return {
        "node_id": NODE_ID, "season_number": season, "hours_online": hours,
        "data_hash": "a" * 64, "oracle_sig": "b" * 64,
        "dl_tx_id": "0x" + "c" * 64, "dl_key_hex": "6174746573740000",
    }


def _register(client):
    return client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})


def test_matching_hours_flagged_match(client):
    assert _register(client).status_code == 201
    _seed_uptime(client, NODE_ID, CLOSED_SEASON, 3)
    j = client.post("/attestations", json=_attest_body(CLOSED_SEASON, 3)).json()
    assert j["oracle_hours_online"] == 3
    assert j["hours_match"] is True


def test_mismatch_flagged_and_audited(client):
    _register(client)
    _seed_uptime(client, NODE_ID, CLOSED_SEASON, 3)
    r = client.post("/attestations", json=_attest_body(CLOSED_SEASON, 24))
    assert r.status_code == 201  # not rejected — the value IS what's on chain
    j = r.json()
    assert j["oracle_hours_online"] == 3
    assert j["hours_match"] is False
    ev = client.get("/audit").json()
    mm = [e for e in ev if e["action"] == "attestation.hours_mismatch"]
    assert len(mm) == 1
    assert mm[0]["detail"]["reported_hours"] == 24
    assert mm[0]["detail"]["oracle_hours"] == 3
    assert mm[0]["actor"] == "writer"


def test_in_progress_season_not_flagged(client):
    _register(client)
    cur = seasons.current_season()
    j = client.post("/attestations", json=_attest_body(cur, 5)).json()
    # In-progress season count still changes — no cross-check assertion.
    assert j["hours_match"] is None
    assert j["oracle_hours_online"] is None


def test_uptime_route_and_check_agree(client):
    _register(client)
    _seed_uptime(client, NODE_ID, CLOSED_SEASON, 4)
    up = client.get(f"/uptime/{NODE_ID}/{CLOSED_SEASON}").json()
    j = client.post("/attestations", json=_attest_body(CLOSED_SEASON, up["hours_online"])).json()
    assert j["hours_match"] is True  # shared calc => they agree by construction
