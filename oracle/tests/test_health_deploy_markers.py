# SPDX-License-Identifier: Apache-2.0
""""Did my deploy land?" must be answerable from outside the box.

On 2026-08-08 that question was answered twice by SSHing to the oracle host and
grepping source files — and the first answer was WRONG, because a deploy that
omitted orchard_chia/ looked successful when nothing could show what was
actually running.

The markers ride as RESPONSE HEADERS rather than in a body, for three reasons
learned the hard way:
  * /health's body is a contract an existing test pins to exactly {"ok": true},
    and liveness must stay dependency-free;
  * `/` and `/health/ready` are answered with a 403 challenge to script clients,
    so neither can be used from outside;
  * a new path would be a bet on opaque edge rules.
A header sidesteps all three and works on whichever endpoint the edge allows.

The source marker is a hash of the files on disk, NOT `git rev-parse HEAD`: the
deploy is `git checkout origin/main -- oracle/`, which updates files without
moving HEAD, so HEAD would report the previous commit while new code ran. A
marker that lies is worse than none.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oracle.app import db
from oracle.app.main import app
from oracle.app.routes import health as H


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    return TestClient(app)


def test_the_health_body_contract_is_untouched(client):
    """The whole point of using headers: this must not change."""
    assert client.get("/health").json() == {"ok": True}


def test_every_response_carries_the_deploy_markers(client):
    for path in ("/health", "/nodes", "/network/stats"):
        r = client.get(path)
        assert r.headers.get("X-Orchard-Source"), f"{path} carries no source marker"
        assert r.headers.get("X-Orchard-Schema"), f"{path} carries no schema marker"


def test_the_markers_are_on_reachable_endpoints_specifically(client):
    """`/` and `/health/ready` are 403'd at the edge, so the marker cannot live
    only there. /nodes is proven reachable from outside."""
    assert client.get("/nodes").headers.get("X-Orchard-Source")


def test_the_source_marker_changes_when_the_source_changes():
    """A marker that never moves cannot prove a deploy landed."""
    before = H._source_fingerprint()
    src = Path(H.__file__)
    original = src.read_bytes()
    try:
        src.write_bytes(original + b"\n# probe\n")
        assert H._source_fingerprint() != before
    finally:
        src.write_bytes(original)
    assert H._source_fingerprint() == before, "restoring the source must restore the hash"


def test_the_source_marker_is_stable_across_requests(client):
    seen = {client.get("/health").headers["X-Orchard-Source"] for _ in range(5)}
    assert len(seen) == 1
    assert len(seen.pop()) == 12


def test_code_and_schema_are_reported_separately(client):
    """They deploy together but land apart: a checkout without a restart leaves
    new code unloaded; a restart without a migration leaves the schema behind."""
    r = client.get("/health")
    assert r.headers["X-Orchard-Source"] != r.headers["X-Orchard-Schema"]


def test_liveness_survives_a_dead_database(client, monkeypatch):
    """The property the pre-existing test protects, asserted here too.

    The schema head is captured at STARTUP, so a database that dies afterwards
    cannot make a liveness poll fail or slow down.
    """
    def boom():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(H.db, "engine", boom)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert r.headers.get("X-Orchard-Source"), "markers must survive a DB outage"


def test_the_schema_head_is_read_at_startup_not_per_request(client, monkeypatch):
    """Querying per request would quietly turn liveness into readiness."""
    calls = {"n": 0}
    real = H.db.engine

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(H.db, "engine", counting)
    for _ in range(10):
        client.get("/health")
    assert calls["n"] == 0, (
        f"/health touched the database {calls['n']} times; it must serve the "
        f"startup-captured value"
    )


def test_priming_reads_a_real_revision(tmp_path, monkeypatch):
    """Against a genuinely migrated DB — with create_all() there is no
    alembic_version table and 'unknown' would pass for the wrong reason."""
    from alembic import command
    from alembic.config import Config

    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{(tmp_path/'m.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()

    repo = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo / "oracle" / "migrations"))
    command.upgrade(cfg, "head")

    head = H.prime_schema_head()
    assert head not in ("unknown", "none"), f"expected a revision, got {head!r}"
    assert H.schema_head() == head
