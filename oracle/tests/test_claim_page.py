# SPDX-License-Identifier: Apache-2.0
"""Tests for the hosted browser claim page (GET /claim + GET /claim/config).

The page itself drives the existing /auth + /provision endpoints client-side
(WalletConnect), which can't be unit-tested without a real wallet; these just
assert the page is served and the config endpoint reflects
ORCHARD_ORACLE_WC_PROJECT_ID so the browser gets a usable (or honestly-empty)
WalletConnect config.
"""
from __future__ import annotations

import os

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient

from oracle.app.config import reset_settings_for_tests
from oracle.app.main import app


def test_claim_page_serves_html():
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/claim")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # key bits of the flow the page must drive
    assert "Claim your Tree" in r.text
    assert "/provision/claim" in r.text
    assert "/auth/challenge" in r.text
    assert "chia_signMessageByAddress" in r.text
    # UX: code preview + a real post-claim destination (not a dead end)
    assert "code-preview" in r.text
    assert "Back to The Orchard" in r.text
    assert "Tree claimed!" in r.text
    # "Your Trees" list (session-scoped /nodes) so operators see existing Trees
    assert "Your Trees" in r.text
    assert "trees-card" in r.text
    assert '"/nodes"' in r.text


def test_claim_config_unconfigured_by_default(monkeypatch):
    monkeypatch.delenv("ORCHARD_ORACLE_WC_PROJECT_ID", raising=False)
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/claim/config")
    assert r.status_code == 200
    j = r.json()
    assert j["wc_configured"] is False
    assert j["wc_project_id"] == ""
    assert j["metadata"]["name"].startswith("The Orchard")
    assert j["metadata"]["url"].startswith("http")
    # navigation targets the page reads (home defaults to the landing page;
    # dashboard hidden until the operator sets it)
    assert j["home_url"] == "https://theorchard.network"
    assert j["dashboard_url"] == ""
    reset_settings_for_tests()


def test_claim_config_reflects_project_id(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_WC_PROJECT_ID", "pid_test_abc123")
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/claim/config")
    j = r.json()
    assert j["wc_configured"] is True
    assert j["wc_project_id"] == "pid_test_abc123"
    reset_settings_for_tests()  # don't leak the env into other tests
