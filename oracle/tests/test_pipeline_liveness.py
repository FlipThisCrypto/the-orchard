# SPDX-License-Identifier: Apache-2.0
"""Pipeline liveness is visible from outside.

Readings flowing while attestation goes quiet was invisible: exit codes and
ops journals live on the operator's box, so a stalled writer looked exactly
like a healthy quiet one to every external observer. /network/stats now says
when the writer last recorded an attestation and when any Tree last posted,
so a heartbeat can alert on "readings loud, chain quiet".
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from oracle.app import db, models
from oracle.app.main import app

NODE = "D8641AD6CAE36977818499469F7E8C49"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL",
                       f"sqlite:///{(tmp_path/'l.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    from oracle.app.routes import network
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    network.reset_cache_for_tests()
    now = dt.datetime.now(dt.timezone.utc)
    s = db.session_factory()()
    s.add(models.Node(node_id=NODE, signing_key_hex="ab" * 32, last_seq=0,
                      registered_at=now))
    s.commit(); s.close()
    return TestClient(app)


def test_an_empty_pipeline_reports_nulls_not_zeros(client):
    j = client.get("/network/stats").json()
    assert j["last_attestation_at"] is None
    assert j["last_reading_at"] is None


def test_liveness_timestamps_surface(client):
    from oracle.app.routes import network
    now = dt.datetime.now(dt.timezone.utc)
    s = db.session_factory()()
    s.add(models.Reading(node_id=NODE, received_at=now,
                         payload_json="{}", sig_hex="ab" * 32))
    s.add(models.Attestation(
        node_id=NODE, season_number=74, hours_online=9,
        data_hash="ab" * 32, oracle_sig="cd" * 32,
        written_to_datalayer_at=now - dt.timedelta(hours=3)))
    s.commit(); s.close()
    network.reset_cache_for_tests()

    j = client.get("/network/stats").json()
    assert j["last_reading_at"] is not None
    assert j["last_attestation_at"] is not None
    # The heartbeat's condition: readings recent, chain older.
    assert j["last_attestation_at"] < j["last_reading_at"]
