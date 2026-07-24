# SPDX-License-Identifier: Apache-2.0
"""Oracle request-observability layer: request ids, metrics, error capture."""
from __future__ import annotations

import os

# In-memory DB so importing the app never touches the real oracle.db.
os.environ.setdefault("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")

from fastapi.testclient import TestClient  # noqa: E402

from oracle.app import observability  # noqa: E402
from oracle.app.main import app  # noqa: E402
from oracle.app.routes import health as health_route  # noqa: E402


def test_every_response_carries_request_id():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) == 16


def test_incoming_request_id_is_preserved():
    client = TestClient(app)
    r = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers.get("X-Request-ID") == "trace-abc-123"


def test_metrics_count_requests_and_group_by_route():
    observability.METRICS.reset()
    client = TestClient(app)
    for _ in range(3):
        client.get("/health")
    snap = client.get("/metrics").json()
    assert snap["requests_total"] >= 3
    # /health requests grouped under one normalized route label.
    assert "GET /health" in snap["routes"]
    assert snap["routes"]["GET /health"]["count"] == 3
    assert snap["by_status_class"].get("2xx", 0) >= 3


def test_metrics_normalizes_dynamic_segments():
    # Two different node ids collapse to one :id route bucket.
    assert observability.route_label("GET", "/readings/ABCDEF0123456789") == "GET /readings/:id"
    assert observability.route_label("GET", "/uptime/DEADBEEFCAFE0001/5") == "GET /uptime/:id/:id"


def test_metrics_is_loopback_only(monkeypatch):
    client = TestClient(app)
    # Simulate a remote client host (TestClient defaults to loopback 'testclient').
    r = client.get("/metrics", headers={"host": "oracle.local"})
    # host header doesn't change request.client.host, so patch it:
    from starlette.requests import Request

    orig = Request.client.fget

    class _Remote:
        host = "10.0.0.9"
        port = 5000

    monkeypatch.setattr(Request, "client", property(lambda self: _Remote()))
    r = client.get("/metrics")
    assert r.status_code == 403
    monkeypatch.setattr(Request, "client", property(orig))


def test_readiness_ok_when_db_reachable():
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["db"] == "ok"


def test_readiness_503_when_db_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    # Break the DB engine access the readiness probe uses.
    monkeypatch.setattr(health_route.db, "engine", boom)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert "error" in body["checks"]["db"]


def test_liveness_stays_cheap_and_dependency_free(monkeypatch):
    # Even with the DB broken, liveness must still report ok (process is up).
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(health_route.db, "engine", boom)
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}


def test_unhandled_exception_returns_clean_500_with_request_id(monkeypatch):
    observability.METRICS.reset()
    # Make the root endpoint raise, then confirm the middleware turns it into a
    # structured 500 (not a leaked stack trace) and counts it as an error.
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(health_route.seasons, "current_season", boom)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/")
    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "internal server error"
    assert body["request_id"] == r.headers.get("X-Request-ID")
    snap = observability.METRICS.snapshot()
    assert snap["errors_total"] >= 1
    assert snap["by_status_class"].get("5xx", 0) >= 1
