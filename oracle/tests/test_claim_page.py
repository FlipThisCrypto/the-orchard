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
    # the WC flow now lives in the shared widget, included via /connect.js
    assert "/connect.js" in r.text
    assert "data-orchard-connect" in r.text
    # UX: code preview + a real post-claim destination (not a dead end)
    assert "code-preview" in r.text
    assert "Back to The Orchard" in r.text
    assert "Tree claimed!" in r.text
    # "Your Trees" list (session-scoped /nodes) so operators see existing Trees
    assert "Your Trees" in r.text
    assert "trees-card" in r.text
    assert '"/nodes"' in r.text


def test_connect_widget_served():
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/connect.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    # the shared widget's contract
    assert "OrchardConnect" in r.text
    assert "orchard_session" in r.text            # the shared cookie
    assert "chia_signMessageByAddress" in r.text  # the WC flow lives here now
    assert "/auth/verify" in r.text


def test_cors_allows_orchard_origin():
    reset_settings_for_tests()
    with TestClient(app) as c:
        # a CORS preflight from the landing page origin
        r = c.options("/auth/challenge", headers={
            "Origin": "https://theorchard.network",
            "Access-Control-Request-Method": "POST",
        })
    assert r.headers.get("access-control-allow-origin") == "https://theorchard.network"


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
    # dashboard defaults to the public Orchard View so the "View your Tree"
    # button + per-Tree "View live" links are wired out of the box)
    assert j["home_url"] == "https://theorchard.network"
    assert j["dashboard_url"] == "https://view.theorchard.network"
    reset_settings_for_tests()


# --- GET /claim/pass/{address} — the flasher's pre-flash Pass gate ---------

_ADDR = "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"


def test_claim_pass_check_has_pass(monkeypatch):
    from oracle.app import pass_verify
    monkeypatch.setattr(
        pass_verify, "list_passes_for_address",
        lambda addr: [{"nft_coin_id": "nft1examplepass",
                       "name": "Orchard Pass", "edition_number": 1}],
    )
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get(f"/claim/pass/{_ADDR}")
    assert r.status_code == 200
    j = r.json()
    assert j["has_pass"] is True
    assert j["pass_nft_id"] == "nft1examplepass"
    assert j["pass_name"] == "Orchard Pass"
    assert j["edition_number"] == 1
    assert j["mintgarden_url"].endswith("nft1examplepass")


def test_claim_pass_check_no_pass(monkeypatch):
    from oracle.app import pass_verify
    monkeypatch.setattr(pass_verify, "list_passes_for_address", lambda addr: [])
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get(f"/claim/pass/{_ADDR}")
    assert r.status_code == 200
    j = r.json()
    assert j["has_pass"] is False
    assert j["pass_nft_id"] is None
    assert "mintgarden.io/collections/" in j["buy_url"]


def test_claim_pass_check_rejects_bad_address():
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/claim/pass/not-an-address")
    assert r.status_code == 400


def test_claim_pass_check_indexer_error_502(monkeypatch):
    from oracle.app import pass_verify

    def boom(addr):
        raise pass_verify.PassVerifyError("indexer down")

    monkeypatch.setattr(pass_verify, "list_passes_for_address", boom)
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get(f"/claim/pass/{_ADDR}")
    assert r.status_code == 502


def test_claim_config_reflects_project_id(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_WC_PROJECT_ID", "pid_test_abc123")
    reset_settings_for_tests()
    with TestClient(app) as c:
        r = c.get("/claim/config")
    j = r.json()
    assert j["wc_configured"] is True
    assert j["wc_project_id"] == "pid_test_abc123"
    reset_settings_for_tests()  # don't leak the env into other tests
