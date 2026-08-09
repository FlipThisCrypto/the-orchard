# SPDX-License-Identifier: Apache-2.0
"""/health must answer "did my deploy land?" from outside the box.

On 2026-08-08 that question was answered twice by SSHing to the oracle host and
grepping source files — and the first answer was wrong, because a deploy that
omitted orchard_chia/ looked successful when nothing could show what was
actually running. /health returned {"ok": true} and nothing else. `/` carries a
version but is answered with a 403 challenge to script clients, so it cannot be
used from outside.

The marker is a hash of the source files on disk, NOT `git rev-parse HEAD`. The
deploy is `git checkout origin/main -- oracle/`, which updates files without
moving HEAD — so HEAD would keep reporting the previous commit while new code
ran. A version marker that lies is worse than none.
"""
from __future__ import annotations

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
    monkeypatch.setattr(H, "_SCHEMA_HEAD", None)   # clear the per-process cache
    db.create_all()
    return TestClient(app)


def test_health_still_reports_liveness(client):
    body = client.get("/health").json()
    assert body["ok"] is True, "the original contract must not change"


def test_health_carries_deploy_markers(client):
    body = client.get("/health").json()
    for field in ("source", "datalayer_schema", "schema_head"):
        assert field in body, f"/health must expose {field} to be useful from outside"
    assert len(body["source"]) == 12


def test_the_fingerprint_changes_when_the_source_changes(tmp_path):
    """A marker that never moves cannot prove a deploy landed."""
    before = H._source_fingerprint()
    src = __import__("pathlib").Path(H.__file__)
    original = src.read_bytes()
    try:
        src.write_bytes(original + b"\n# probe\n")
        assert H._source_fingerprint() != before
    finally:
        src.write_bytes(original)
    assert H._source_fingerprint() == before, "restoring the source must restore the hash"


def test_the_fingerprint_is_stable_across_calls(client):
    """It must identify a build, not vary per request."""
    seen = {client.get("/health").json()["source"] for _ in range(5)}
    assert len(seen) == 1


def test_code_and_schema_are_reported_separately(client):
    """They deploy together but can land apart.

    A checkout without a restart leaves new code unloaded; a restart without a
    migration leaves the schema behind. One field cannot express both, and
    conflating them is how "it deployed" gets believed when only half did.
    """
    body = client.get("/health").json()
    assert body["source"] != body["schema_head"]


def test_health_survives_a_broken_database(client, monkeypatch):
    """Liveness must not fail because a dependency is down — that is readiness."""
    monkeypatch.setattr(H, "_SCHEMA_HEAD", None)

    def boom():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(H.db, "engine", boom)
    r = client.get("/health")
    assert r.status_code == 200, "a DB outage must not take liveness down"
    assert r.json()["ok"] is True
    assert r.json()["schema_head"] == "unknown"


def test_a_transient_db_error_is_not_cached_forever(tmp_path, monkeypatch):
    """An outage during the first poll must not pin 'unknown' for the process.

    Needs a genuinely MIGRATED database: with create_all() there is no
    alembic_version table, so "unknown" is the correct answer every time and
    the test would pass for the wrong reason.
    """
    from pathlib import Path
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

    monkeypatch.setattr(H, "_SCHEMA_HEAD", None)
    c = TestClient(app)

    # Sanity: a healthy DB really does report a revision, or the test below
    # proves nothing.
    assert c.get("/health").json()["schema_head"] != "unknown"

    monkeypatch.setattr(H, "_SCHEMA_HEAD", None)
    calls = {"n": 0}
    real_engine = H.db.engine

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_engine()

    monkeypatch.setattr(H.db, "engine", flaky)
    assert c.get("/health").json()["schema_head"] == "unknown"
    monkeypatch.setattr(H.db, "engine", real_engine)
    assert c.get("/health").json()["schema_head"] != "unknown", (
        "a transient failure must not be cached as the answer"
    )
