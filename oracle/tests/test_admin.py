# SPDX-License-Identifier: Apache-2.0
"""Tests for the node-admin CLI (oracle.app.admin).

Covers the delete/keep target resolution, the dry-run-vs-apply boundary,
and that a delete cascades across readings / uptime_hours / attestations
with no orphaned child rows left behind.
"""
from __future__ import annotations

import os

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oracle.app import admin, db as dbmod
from oracle.app import models  # noqa: F401 — registers all tables on Base.metadata
from oracle.app.config import reset_settings_for_tests
from oracle.app.db import Base, reset_for_tests


@pytest.fixture()
def eng(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")
    reset_settings_for_tests()
    reset_for_tests()
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool, future=True)
    # Make the admin module + db helpers use this exact engine.
    monkeypatch.setattr(dbmod, "_engine", e, raising=False)
    Base.metadata.create_all(e)
    _seed(e)
    return e


def _seed(e):
    """Three nodes; each with readings + uptime; node A also has an attestation.
    Seed through the ORM so model-side defaults (registered_at, received_at,
    last_seq, …) are applied — raw INSERTs would trip the NOT NULL columns."""
    Session = sessionmaker(bind=e, future=True)
    with Session() as s:
        for nid in ("AA" * 16, "BB" * 16, "CC" * 16):
            s.add(models.Node(node_id=nid, signing_key_hex="00" * 32, label=f"tree-{nid[:2]}"))
            for i in range(3):
                s.add(models.Reading(node_id=nid, payload_json="{}", sig_hex=f"{nid[:2]}{i}"))
            s.add(models.UptimeHour(node_id=nid, hour_utc="2026-06-15T06", reading_count=3))
        s.add(models.Attestation(node_id="AA" * 16, season_number=1, hours_online=1,
                                 data_hash="ab" * 32))
        s.commit()


def _node_ids(e):
    with e.connect() as c:
        return {r[0] for r in c.execute(text("SELECT node_id FROM nodes"))}


def _orphans(e):
    with e.connect() as c:
        return {t: c.execute(text(
            f"SELECT count(*) FROM {t} WHERE node_id NOT IN (SELECT node_id FROM nodes)"
        )).scalar() for t in admin._CHILD_TABLES}


def test_keep_deletes_the_rest_and_cascades(eng):
    rc = admin.cmd_modify(eng, "keep", ["AA" * 16], apply=True)
    assert rc == 0
    assert _node_ids(eng) == {"AA" * 16}
    # No child rows left pointing at the deleted nodes.
    assert _orphans(eng) == {"readings": 0, "uptime_hours": 0, "attestations": 0}
    # Kept node's own rows survive.
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM readings")).scalar() == 3


def test_delete_named_node(eng):
    rc = admin.cmd_modify(eng, "delete", ["bb" * 16], apply=True)  # lowercase ok
    assert rc == 0
    assert _node_ids(eng) == {"AA" * 16, "CC" * 16}


def test_dry_run_changes_nothing(eng):
    before = _node_ids(eng)
    rc = admin.cmd_modify(eng, "keep", ["AA" * 16], apply=False)
    assert rc == 0
    assert _node_ids(eng) == before  # all three still present


def test_keep_with_no_valid_ids_refuses(eng):
    before = _node_ids(eng)
    rc = admin.cmd_modify(eng, "keep", ["DD" * 16], apply=True)
    assert rc == 2  # refused — would have deleted everything
    assert _node_ids(eng) == before
