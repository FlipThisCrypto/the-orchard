# SPDX-License-Identifier: Apache-2.0
"""Tests for claim-code provisioning (ADR-0005 / T9): announce -> claim -> poll.

Runs against the real app via TestClient with an in-memory DB. Uses the legacy
body-wallet path (require_wallet_session=false) and stubs the on-chain Pass
lookup, so the claim flow is exercised without a full WalletConnect round-trip.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oracle.app import models
from oracle.app.config import reset_settings_for_tests
from oracle.app.db import Base, get_db, reset_for_tests
from oracle.app.main import app

NODE_ID = "0123456789ABCDEF0123456789ABCDEF"
KEY_HEX = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF"
WALLET = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
PASS_NFT = "nft1n00ugdl737xc6ht4yjdc3cer047lcz9actdxfzpxyat3tsu72z0q46g56z"


@pytest.fixture()
def prov(monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    reset_settings_for_tests()
    reset_for_tests()
    # Stub the on-chain Pass lookup: wallet holds a Pass by default.
    from oracle.app import pass_verify
    if hasattr(pass_verify, "clear_cache"):
        pass_verify.clear_cache()
    monkeypatch.setattr(pass_verify, "first_pass_nft_id", lambda addr: PASS_NFT)

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
        yield c, Session, monkeypatch, pass_verify
    app.dependency_overrides.clear()


def _announce(c, code="ABCD2345"):
    return c.post("/provision/announce", json={
        "node_id": NODE_ID, "signing_key_hex": KEY_HEX, "claim_code": code, "label": "tree-1"})


def _claim(c, code="ABCD2345", wallet=WALLET):
    return c.post("/provision/claim", json={"claim_code": code, "wallet_address": wallet})


def test_announce_claim_poll_happy_path(prov):
    c, Session, *_ = prov
    r = _announce(c)
    assert r.status_code == 201 and r.json()["claimed"] is False
    # Node exists, unprovisioned (no wallet yet).
    with Session() as s:
        n = s.get(models.Node, NODE_ID)
        assert n is not None and n.wallet_address is None

    assert c.get("/provision/ABCD2345").json() == {
        "claimed": False, "known": True, "node_id": None, "label": "tree-1"}

    r = _claim(c)
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is True and r.json()["pass_nft_id"] == PASS_NFT

    poll = c.get("/provision/abcd-2345")  # normalization: lowercase + dash ok
    assert poll.json()["claimed"] is True and poll.json()["node_id"] == NODE_ID
    with Session() as s:
        n = s.get(models.Node, NODE_ID)
        assert n.wallet_address == WALLET and n.pass_nft_id == PASS_NFT


def test_announce_rejects_degenerate_signing_key(prov):
    c, *_ = prov
    r = c.post("/provision/announce", json={
        "node_id": NODE_ID, "signing_key_hex": "00" * 32,
        "claim_code": "ABCD2345", "label": "tree-1"})
    assert r.status_code == 422


def test_announce_rejects_non_crockford_claim_code(prov):
    c, *_ = prov
    # Contains I and O — not Crockford (ambiguous with 1/0).
    r = c.post("/provision/announce", json={
        "node_id": NODE_ID, "signing_key_hex": KEY_HEX,
        "claim_code": "ABCDIO12", "label": "tree-1"})
    assert r.status_code == 400


def test_claim_unknown_code_404(prov):
    c, *_ = prov
    assert _claim(c, code="ZZZZ9999").status_code == 404


def test_double_claim_409(prov):
    c, *_ = prov
    _announce(c)
    assert _claim(c).status_code == 200
    assert _claim(c).status_code == 409  # single-use


def test_expired_claim_410(prov):
    c, Session, *_ = prov
    _announce(c)
    with Session() as s:  # backdate the expiry
        cl = s.get(models.Claim, "ABCD2345")
        cl.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        s.commit()
    assert _claim(c).status_code == 410


def test_claim_without_pass_403(prov):
    c, Session, monkeypatch, pass_verify = prov
    monkeypatch.setattr(pass_verify, "first_pass_nft_id", lambda addr: None)  # no Pass
    _announce(c)
    assert _claim(c).status_code == 403


def test_announce_conflicting_key_409(prov):
    c, *_ = prov
    _announce(c)
    r = c.post("/provision/announce", json={
        "node_id": NODE_ID, "signing_key_hex": "FF" * 32, "claim_code": "WXYZ7777"})
    assert r.status_code == 409


def test_announce_after_claim_reports_claimed(prov):
    c, *_ = prov
    _announce(c)
    _claim(c)
    # Tree reboots and re-announces -> oracle says it's already claimed.
    r = _announce(c)
    assert r.status_code == 201 and r.json()["claimed"] is True
