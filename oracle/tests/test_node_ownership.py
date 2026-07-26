# SPDX-License-Identifier: Apache-2.0
"""Ownership guard on re-registration.

Regression for a confirmed cross-operator takeover: knowing a Tree's signing key
(readable over USB, returned by the dashboard's own /api/serial/identify, stored
plaintext in the DB) was sufficient to re-register someone else's Tree under
your own wallet and redirect every future $JUICE payout.
"""
from __future__ import annotations

import datetime
import os

os.environ.setdefault("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from oracle.app import models, pass_verify, sessions  # noqa: E402
from oracle.app.config import reset_settings_for_tests  # noqa: E402
from oracle.app.db import Base, get_db, reset_for_tests  # noqa: E402
from oracle.app.main import app  # noqa: E402

NODE = "AABBCCDDEEFF00112233445566778899"
KEY = "11" * 32
ALICE = "xch1alice00000000000000000000000000000000000000000000000000aaaa"
MALLORY = "xch1mallory000000000000000000000000000000000000000000000000bbbb"


@pytest.fixture()
def client(monkeypatch):
    # The on-chain Pass check is network-dependent and not what these tests
    # cover — stub it so ownership logic is isolated.
    monkeypatch.setattr(pass_verify, "first_pass_nft_id", lambda addr: "nft1fakepass")
    monkeypatch.setattr(
        pass_verify, "utcnow",
        lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    reset_settings_for_tests()
    reset_for_tests()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng, autoflush=False, future=True)

    def _ov():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _ov
    c = TestClient(app)
    c.Session = TS  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _register(client, token):
    return client.post(
        "/register",
        json={"node_id": NODE, "signing_key_hex": KEY},
        headers={"Authorization": f"Bearer {token}"},
    )


def _owner(client):
    with client.Session() as s:
        return s.get(models.Node, NODE).wallet_address


def test_foreign_wallet_cannot_take_over_a_bound_tree(client):
    tok_a, _ = sessions.issue(ALICE)
    tok_m, _ = sessions.issue(MALLORY)

    assert _register(client, tok_a).status_code == 201
    assert _owner(client) == ALICE

    r = _register(client, tok_m)          # same device key, different wallet
    assert r.status_code == 409
    assert "already bound to a different wallet" in r.json()["detail"]
    assert _owner(client) == ALICE, "ownership must not transfer"


def test_blocked_takeover_is_audited(client):
    tok_a, _ = sessions.issue(ALICE)
    tok_m, _ = sessions.issue(MALLORY)
    _register(client, tok_a)
    _register(client, tok_m)

    events = client.get("/audit", params={"node_id": NODE}).json()
    blocked = [e for e in events if e["action"] == "node.takeover_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["actor"] == MALLORY


def test_owner_can_still_reregister(client):
    """The guard must not break legitimate re-provisioning."""
    tok_a, _ = sessions.issue(ALICE)
    assert _register(client, tok_a).status_code == 201
    r = _register(client, tok_a)
    assert r.status_code == 201
    assert r.json()["new"] is False
    assert _owner(client) == ALICE


def test_unclaimed_tree_can_still_be_claimed(client):
    """A legacy/unowned node (wallet_address NULL) is still claimable — that is
    the normal onboarding path and must keep working."""
    monkey_client = client
    # Register with no session at all (legacy path) so no wallet is bound.
    reset_settings_for_tests()
    r = monkey_client.post(
        "/register", json={"node_id": NODE, "signing_key_hex": KEY}
    )
    # Default policy requires a session; if so, seed an unowned node directly.
    if r.status_code != 201:
        with monkey_client.Session() as s:
            s.add(models.Node(node_id=NODE, signing_key_hex=KEY))
            s.commit()
    assert _owner(monkey_client) is None

    tok_a, _ = sessions.issue(ALICE)
    assert _register(monkey_client, tok_a).status_code == 201
    assert _owner(monkey_client) == ALICE
