# SPDX-License-Identifier: Apache-2.0
"""Refused ingest is countable, so device-quiet and oracle-refusing separate.

The live ingest rate dropped 75% the day the new defaults deployed, and
nothing could say whether the device had gone quiet or the oracle was
refusing its posts — rejections vanished into access logs. (It was the
device; proving that took inspecting arrival gaps by hand.) Now every refusal
lands in a bounded per-day, per-reason counter and /network/stats exposes the
24h totals.
"""
from __future__ import annotations

import datetime as dt
import hmac as hmac_mod
import json
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from oracle.app import db, models
from oracle.app.main import app

NODE = "D8641AD6CAE36977818499469F7E8C49"
KEY = "ab" * 32


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL",
                       f"sqlite:///{(tmp_path/'r.db').as_posix()}")
    # Other test modules flip these process-wide at import; pin ours.
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_SEQ", "true")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    from oracle.app.routes import network
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    network.reset_cache_for_tests()
    s = db.session_factory()()
    s.add(models.Node(node_id=NODE, signing_key_hex=KEY, last_seq=0,
                      registered_at=dt.datetime.now(dt.timezone.utc)))
    s.commit(); s.close()
    return TestClient(app)


def _post(client, body_obj, *, node=NODE, key=KEY):
    body = json.dumps(body_obj).encode()
    sig = hmac_mod.new(bytes.fromhex(key), body, sha256).hexdigest().upper()
    return client.post("/readings", content=body,
                       headers={"X-Orchard-Node": node, "X-Orchard-Sig": sig,
                                "Content-Type": "application/json"})


def _counters(client):
    from oracle.app.routes import network
    network.reset_cache_for_tests()
    return client.get("/network/stats").json()


def test_a_replayed_seq_is_counted(client):
    assert _post(client, {"seq": 5, "sensors": {}}).status_code == 202
    r = _post(client, {"seq": 4, "sensors": {}})
    assert r.status_code == 409
    j = _counters(client)
    assert j["readings_rejected_24h"] == 1
    assert j["reject_reasons_24h"] == {"replayed-seq": 1}


def test_a_missing_seq_is_counted(client):
    r = _post(client, {"sensors": {}})
    assert r.status_code == 400
    assert _counters(client)["reject_reasons_24h"] == {"missing-seq": 1}


def test_a_bad_hmac_is_counted(client):
    r = _post(client, {"seq": 1, "sensors": {}}, key="cd" * 32)
    assert r.status_code == 401
    assert _counters(client)["reject_reasons_24h"] == {"bad-hmac": 1}


def test_an_unregistered_node_is_counted(client):
    r = _post(client, {"seq": 1, "sensors": {}}, node="EE" * 16)
    assert r.status_code == 404
    assert _counters(client)["reject_reasons_24h"] == {"unregistered-node": 1}


def test_accepted_readings_count_nothing(client):
    assert _post(client, {"seq": 1, "sensors": {}}).status_code == 202
    j = _counters(client)
    assert j["readings_rejected_24h"] == 0 and j["reject_reasons_24h"] == {}


def test_the_count_survives_the_rejected_requests_rollback(client):
    """The refusal aborts its transaction; the counter must not ride in it."""
    # Bodies must DIFFER: an identical body is the exact-duplicate path
    # (idempotent 202) before seq is ever judged. Distinct bodies with a
    # non-advancing seq are the true replay shape.
    assert _post(client, {"seq": 9, "sensors": {"t": 1}}).status_code == 202
    for t in (2, 3):
        assert _post(client, {"seq": 9, "sensors": {"t": t}}).status_code == 409
    assert _counters(client)["reject_reasons_24h"] == {"replayed-seq": 2}
