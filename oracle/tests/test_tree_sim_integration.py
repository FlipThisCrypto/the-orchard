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


@pytest.fixture()
def seq_oracle_client(monkeypatch):
    """Oracle with require_seq=true (production target posture for v0.5+)."""
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_SEQ", "true")
    # Default future-skew (300s) + a tight age window so stale_ts is 422.
    monkeypatch.setenv("ORCHARD_ORACLE_MAX_READING_AGE_SECONDS", "600")
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


def test_simulator_negative_modes_rejected(seq_oracle_client):
    """Adversarial modes must not credit uptime / must fail closed.

    This is the CI gate for the tree_sim --mode negative suite: replay,
    bad signatures, unknown nodes, oversized bodies, and missing seq.
    """
    from tools.tree_sim.sim import run_negative

    client, _ = seq_oracle_client
    stats = run_negative(client, verbose=False, require_seq=True)
    assert stats["failures"] == [], stats["failures"]
    # Spot-check the hard security failures.
    assert stats["results"]["duplicate_seq"] == 409
    assert stats["results"]["decreasing_seq"] == 409
    assert stats["results"]["invalid_sig"] == 401
    assert stats["results"]["wrong_key"] == 401
    assert stats["results"]["unknown_node"] == 404
    assert stats["results"]["missing_seq"] == 400
    assert stats["results"]["oversized"] == 413
    assert stats["results"]["future_ts"] == 422
    assert stats["results"]["stale_ts"] == 422


def test_simulator_require_seq_rejects_stale_after_happy_path(seq_oracle_client):
    client, _ = seq_oracle_client
    tree = VirtualTree.random(0)
    assert client.register(tree).status_code in (200, 201)
    assert client.post_reading(tree).status_code == 202  # seq=1
    assert client.post_reading(tree).status_code == 202  # seq=2
    # Crafted stale seq with a new body (not exact-dup).
    r = client.post_mode(tree, "duplicate_seq")
    assert r.status_code == 409
