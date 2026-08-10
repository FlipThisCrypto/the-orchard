# SPDX-License-Identifier: Apache-2.0
"""End-to-end integration test: the Tree simulator against a real oracle app.

This is the integration test the repo previously lacked (HANDOVER T11). It
drives the actual FastAPI app in-process via TestClient — register -> sign ->
POST -> uptime credit — using the same simulator that load-tests a deployed
oracle, so the wire contract (HMAC header, body shape, seq, ts) is exercised
exactly as a real Tree would.
"""
from __future__ import annotations

import os
os.environ.setdefault("ORCHARD_ORACLE_MIN_READINGS_PER_CREDITED_HOUR", "1")  # mechanism tests; quorum pinned in test_uptime_quorum.py

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oracle.app.config import reset_settings_for_tests
from oracle.app.db import Base, get_db, reset_for_tests
from oracle.app.main import app
from tools.tree_sim.sim import OracleClient, VirtualTree, run_functional


@pytest.fixture()
def oracle_client(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    reset_settings_for_tests()
    reset_for_tests()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield OracleClient(client=c), c
    app.dependency_overrides.clear()


def test_simulated_fleet_registers_and_posts(oracle_client):
    client, _ = oracle_client
    stats = run_functional(client, trees=3, rounds=4, verbose=False)
    assert stats["failures"] == [], stats["failures"]
    assert stats["accepted"] == 3 * 4  # every reading 202-accepted


def test_simulated_readings_credit_uptime(oracle_client):
    client, raw = oracle_client
    tree = VirtualTree.random(0)
    assert client.register(tree).status_code in (200, 201)
    for _ in range(5):
        assert client.post_reading(tree).status_code == 202

    season = raw.get("/").json()["current_season"]
    r = raw.get(f"/uptime/{tree.node_id}/{season}")
    assert r.status_code == 200
    # 5 readings in the same wall-clock hour => 1 credited hour, count 5.
    assert r.json()["hours_online"] >= 1


def test_simulator_seq_is_strictly_increasing(oracle_client):
    # The sim must satisfy require_seq (monotonic seq), so it stays a valid
    # load tool once the fleet flag is flipped.
    client, _ = oracle_client
    tree = VirtualTree.random(0)
    bodies = [tree.next_body() for _ in range(4)]
    import json as _json
    seqs = [_json.loads(b)["seq"] for b in bodies]
    assert seqs == [1, 2, 3, 4]
