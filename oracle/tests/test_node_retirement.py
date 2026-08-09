# SPDX-License-Identifier: Apache-2.0
"""Retirement removes a Tree from the living network without losing anything.

Re-flashing a board used to mint a NEW node_id (the web installer erased NVS,
where identity lives), so the oracle accumulated ghost Trees no hardware would
ever claim again — six registered ids for four physical boards, each board
confirmed by asking it NODE_ID over serial.

Deleting them is the wrong answer twice over: it destroys real history, and
DataLayer attestations are permanent and public, so a deleted Tree leaves
on-chain records pointing at a node_id the oracle then denies ever existed.
Retirement says "not part of the living network" without claiming it never was.

The properties that matter, and that these tests pin:
  * a retired Tree disappears from /nodes, trees_registered and trees_active_24h
  * NOTHING it produced is deleted — readings, uptime, attestations, claims
  * it is reversible, exactly
  * the reason is recorded
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from oracle.app import db, models
from oracle.app.main import app

LIVE = "D8641AD6CAE36977818499469F7E8C49"      # ESP32 + temp sensor, fw 0.6.0
GHOST = "E014926F4805D7D848E4EDC32D70E39F"     # registered, never reported


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    now = dt.datetime.now(dt.timezone.utc)
    s = db.session_factory()()
    for nid, last in ((LIVE, now), (GHOST, None)):
        s.add(models.Node(node_id=nid, signing_key_hex="ab" * 32, last_seq=0,
                          registered_at=now, last_reading_at=last))
    s.add(models.Reading(node_id=LIVE, received_at=now,
                         payload_json="{}", sig_hex="ef" * 32))
    s.add(models.Reading(node_id=GHOST, received_at=now,
                         payload_json="{}", sig_hex="ab" * 32))
    s.commit(); s.close()
    return TestClient(app)


def _retire(nid, reason="ghost from a re-flash"):
    from sqlalchemy import text
    from oracle.app.routes import network
    with db.engine().begin() as conn:
        conn.execute(text("UPDATE nodes SET retired_at=:t, retired_reason=:r WHERE node_id=:n"),
                     {"t": dt.datetime.now(dt.timezone.utc).isoformat(), "r": reason, "n": nid})
    # /network/stats is served from a short single-flight cache. Without this
    # the tests would read stale counts and "pass" against the old numbers —
    # and it is worth knowing the same lag exists in production: a retirement
    # shows up in public stats only after the TTL expires.
    network.reset_cache_for_tests()


def test_a_live_tree_is_visible(client):
    ids = {n["node_id"] for n in client.get("/nodes").json()}
    assert {LIVE, GHOST} <= ids


def test_retiring_removes_it_from_the_public_list(client):
    _retire(GHOST)
    ids = {n["node_id"] for n in client.get("/nodes").json()}
    assert GHOST not in ids, "a retired Tree must not appear in the living network"
    assert LIVE in ids, "retiring one Tree must not hide the others"


def test_an_operator_can_still_audit_what_was_retired(client):
    _retire(GHOST)
    ids = {n["node_id"] for n in client.get("/nodes?include_retired=1").json()}
    assert GHOST in ids, "retirement must be auditable, not a disappearance"


def test_network_stats_stop_counting_it(client):
    before = client.get("/network/stats").json()
    assert before["trees_registered"] == 2
    assert before["trees_active_24h"] == 1
    _retire(GHOST)
    after = client.get("/network/stats").json()
    assert after["trees_registered"] == 1, "a ghost must not inflate the network"
    assert after["trees_active_24h"] == 1


def test_retiring_an_ACTIVE_tree_also_drops_the_active_count(client):
    _retire(LIVE)
    s = client.get("/network/stats").json()
    assert s["trees_registered"] == 1
    assert s["trees_active_24h"] == 0, (
        "active counts must exclude retired Trees too, or a retired Tree that "
        "reported recently keeps inflating activity"
    )


def test_nothing_the_tree_produced_is_deleted(client):
    from sqlalchemy import text
    _retire(GHOST)
    with db.engine().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM readings WHERE node_id=:n"),
                         {"n": GHOST}).scalar_one()
        row = conn.execute(text("SELECT node_id FROM nodes WHERE node_id=:n"),
                           {"n": GHOST}).fetchone()
    assert n == 1, "readings must survive retirement — this is not a delete"
    assert row is not None, "the node row itself must survive"


def test_the_reason_is_recorded(client):
    from sqlalchemy import text
    _retire(GHOST, reason="duplicate: same board as D8641AD6, orphaned by erase-flash")
    with db.engine().connect() as conn:
        r = conn.execute(text("SELECT retired_reason FROM nodes WHERE node_id=:n"),
                         {"n": GHOST}).scalar_one()
    assert "duplicate" in r, "an unexplained retirement is a gap in the record"


def test_retirement_is_exactly_reversible(client):
    from sqlalchemy import text
    before = client.get("/network/stats").json()
    _retire(GHOST)
    assert client.get("/network/stats").json()["trees_registered"] == 1
    from oracle.app.routes import network
    with db.engine().begin() as conn:
        conn.execute(text("UPDATE nodes SET retired_at=NULL, retired_reason=NULL "
                          "WHERE node_id=:n"), {"n": GHOST})
    network.reset_cache_for_tests()
    after = client.get("/network/stats").json()
    # Compare everything EXCEPT as_of_utc, which is a clock reading and
    # necessarily differs between two calls. Asserting on it would make the
    # test fail for a reason that has nothing to do with retirement.
    volatile = {"as_of_utc"}
    assert {k: v for k, v in after.items() if k not in volatile} == \
           {k: v for k, v in before.items() if k not in volatile}, (
        "un-retiring must restore the network exactly"
    )
    assert GHOST in {n["node_id"] for n in client.get("/nodes").json()}


def test_a_retired_tree_can_still_be_fetched_directly(client):
    """Its history is public and permanent; hiding it from a direct lookup
    would make already-published DataLayer records unresolvable."""
    _retire(GHOST)
    r = client.get(f"/nodes/{GHOST}")
    assert r.status_code == 200, (
        "on-chain attestations reference this node_id — it must still resolve"
    )
