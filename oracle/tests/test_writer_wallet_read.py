# SPDX-License-Identifier: Apache-2.0
"""The payout must be able to resolve a Tree's recipient wallet.

Regression for a confirmed total payout failure: Phase 6.6 made wallet_address
owner-only on GET /nodes/{id}, but the payout's OracleClient had no way to
authenticate — so every Tree resolved to wallet_address="" and was emitted as
`skipped:no_wallet` while the run printed "nothing to send" and exited 0.

The fix widens disclosure to the operator's OWN writer/payout process (the same
X-Orchard-Writer-Token trust rule POST /attestations uses) WITHOUT re-exposing
the wallet to the public.
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

from oracle.app import models, pass_verify  # noqa: E402
from oracle.app.config import reset_settings_for_tests  # noqa: E402
from oracle.app.db import Base, get_db, reset_for_tests  # noqa: E402
from oracle.app.main import app  # noqa: E402

NODE = "AABBCCDDEEFF00112233445566778899"
KEY = "11" * 32
WALLET = "xch1owner0000000000000000000000000000000000000000000000000aaaa"
TOKEN = "s3cret-writer-token"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(pass_verify, "first_pass_nft_id", lambda addr: "nft1p")
    monkeypatch.setattr(
        pass_verify, "utcnow",
        lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    # Configure a writer token so the loopback fallback is NOT what we measure.
    monkeypatch.setenv("ORCHARD_ORACLE_WRITER_TOKEN", TOKEN)
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
    with TS() as s:
        s.add(models.Node(node_id=NODE, signing_key_hex=KEY, wallet_address=WALLET))
        s.commit()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_public_still_cannot_see_wallet(client):
    """Privacy is preserved: no token => no wallet_address."""
    assert client.get(f"/nodes/{NODE}").json()["wallet_address"] is None
    assert client.get("/nodes").json()[0]["wallet_address"] is None


def test_writer_token_can_read_wallet(client):
    """The payout CAN resolve the recipient."""
    h = {"X-Orchard-Writer-Token": TOKEN}
    assert client.get(f"/nodes/{NODE}", headers=h).json()["wallet_address"] == WALLET
    assert client.get("/nodes", headers=h).json()[0]["wallet_address"] == WALLET


def test_wrong_token_is_not_treated_as_writer(client):
    h = {"X-Orchard-Writer-Token": "wrong"}
    assert client.get(f"/nodes/{NODE}", headers=h).json()["wallet_address"] is None


def test_loopback_alone_does_not_disclose_the_wallet(monkeypatch):
    """Loopback must NOT be sufficient to widen a privacy-sensitive read.

    require_writer accepts bare loopback for the WRITE path, but every process
    on the host — and, behind the tunnel ADR-0004 prescribes, potentially any
    internet caller — presents as loopback. Reusing that rule here would
    silently undo the owner-only wallet scrubbing.
    """
    from oracle.app import session_deps

    monkeypatch.delenv("ORCHARD_ORACLE_WRITER_TOKEN", raising=False)
    reset_settings_for_tests()

    class _Req:  # TestClient's peer host ("testclient") is in LOOPBACK_HOSTS
        client = type("C", (), {"host": "127.0.0.1"})()

    assert session_deps.writer_authenticated(_Req(), None) is False
    assert session_deps.writer_authenticated(_Req(), "anything") is False
    reset_settings_for_tests()


def test_oracle_client_sends_the_writer_header():
    """The payout's client actually presents the credential."""
    from orchard_chia.datalayer.oracle import OracleClient

    seen = {}

    class _Resp:
        status_code = 200
        content = b'{"ok": true}'

        def json(self):
            return {"ok": True}

    def fake_get(url, timeout=None, headers=None, **kw):
        seen["headers"] = headers or {}
        return _Resp()

    import orchard_chia.datalayer.oracle as oc

    orig = oc.requests.get
    oc.requests.get = fake_get
    try:
        OracleClient("http://x", TOKEN)._get("/nodes/ABC")
        assert seen["headers"].get("X-Orchard-Writer-Token") == TOKEN
        seen.clear()
        OracleClient("http://x")._get("/nodes/ABC")
        assert "X-Orchard-Writer-Token" not in seen["headers"]
    finally:
        oc.requests.get = orig


def test_payout_now_resolves_a_wallet_end_to_end(client):
    """The actual failure mode: get_node -> wallet_address -> not skipped."""
    from orchard_chia.datalayer.oracle import OracleClient
    import orchard_chia.datalayer.oracle as oc

    def fake_get(url, timeout=None, headers=None, **kw):
        path = url.split("http://testserver", 1)[-1]
        return _FlaskLike(client.get(path, headers=headers or {}))

    class _FlaskLike:
        def __init__(self, r):
            self._r = r
            self.status_code = r.status_code
            self.content = r.content
            self.text = r.text

        def json(self):
            return self._r.json()

    orig = oc.requests.get
    oc.requests.get = fake_get
    try:
        node = OracleClient("http://testserver", TOKEN).get_node(NODE)
        assert node is not None
        assert (node.get("wallet_address") or "") == WALLET, (
            "payout would emit skipped:no_wallet and pay nobody"
        )
    finally:
        oc.requests.get = orig
