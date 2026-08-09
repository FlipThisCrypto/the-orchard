# SPDX-License-Identifier: Apache-2.0
"""A Tree must be able to join the network without a human touching a database.

The first signing Tree (2026-08-08) signed every reading and never said which
key verified them. So the oracle could not populate ``node.device_pubkey``, the
publisher refused the node as unverifiable, and nothing it measured could be
published — despite 14 perfectly valid signatures sitting in the database.

Recovering the key required ECDSA public-key recovery from the device's own
signatures, then a hand-written UPDATE against the production database. That
works exactly once. With more Trees joining it is neither scalable nor something
that belongs in a provenance chain.

Firmware 0.6.1 sends ``device_pubkey`` at the top level of the reading payload —
inside the HMAC'd body (so only the device secret holder can assert it) and
outside the secp256r1-signed block (so signatures and on-chain bytes are
unchanged). These tests pin the oracle half of that contract.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from oracle.app import db, models
from oracle.app.main import app

SECRET = "ab" * 32
NODE = "AA" * 16
PUB = "03" + "cd" * 32


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    s = db.session_factory()()
    s.add(models.Node(node_id=NODE, signing_key_hex=SECRET, last_seq=0,
                      registered_at=dt.datetime.now(dt.timezone.utc)))
    s.commit()
    s.close()
    return TestClient(app)


def _post(client, payload, node_id=NODE):
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(bytes.fromhex(SECRET), body, hashlib.sha256).hexdigest().upper()
    return client.post("/readings", content=body, headers={
        "Content-Type": "application/json",
        "X-Orchard-Node": node_id,
        "X-Orchard-Sig": sig,
    })


def _reading(seq=1, ts=1786200000000, **over):
    return {"schema": 1, "node_id": NODE, "fw": "0.6.1", "ts_ms": ts, "seq": seq,
            "sensors": {"ds18b20": {"temperature_c": 22.5}}, **over}


def test_a_tree_teaches_the_oracle_its_key_on_the_first_reading(client):
    assert client.get(f"/nodes/{NODE}").json()["device_pubkey"] is None
    assert _post(client, _reading(device_pubkey=PUB)).status_code == 202
    assert client.get(f"/nodes/{NODE}").json()["device_pubkey"] == PUB, (
        "a Tree that reports must become publishable with no human step"
    )


def test_the_key_is_never_rotated(client):
    """Provenance would break: past readings verify against the original key."""
    _post(client, _reading(seq=1, device_pubkey=PUB))
    _post(client, _reading(seq=2, ts=1786200060000, device_pubkey="02" + "ee" * 32))
    assert client.get(f"/nodes/{NODE}").json()["device_pubkey"] == PUB


def test_a_malformed_key_is_refused_not_stored(client):
    for junk in ("nonsense", "", "04" + "cd" * 32, "03" + "cd" * 31, "zz" + "cd" * 32):
        _post(client, _reading(seq=1, device_pubkey=junk))
        assert client.get(f"/nodes/{NODE}").json()["device_pubkey"] is None, (
            f"{junk!r} must not be stored as a provenance key"
        )


def test_a_reading_without_a_key_still_ingests(client):
    """Older firmware must keep working — it just stays unpublishable."""
    assert _post(client, _reading()).status_code == 202
    assert client.get(f"/nodes/{NODE}").json()["device_pubkey"] is None


def test_the_firmware_actually_sends_it():
    """A contract only the oracle honours is not a contract."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "firmware" / "src" / "net" / "oracle.cpp"
    text = src.read_text(encoding="utf-8")
    assert 'payload["device_pubkey"]' in text, "firmware must send device_pubkey"
    assert "p256_pubkey_hex()" in text
    # It must be added before the body is serialised and HMAC'd, or it travels
    # unauthenticated.
    assert text.index('payload["device_pubkey"]') < text.index("serializeJson(payload, body)")
