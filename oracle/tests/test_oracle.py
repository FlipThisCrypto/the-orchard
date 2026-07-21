# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the oracle.

Verifies the end-to-end happy path (register -> sign -> POST -> retrieve)
plus the key failure cases (no sig, bad sig, unknown node).

Tests run against an in-memory SQLite DB via a FastAPI dependency
override; nothing touches the real oracle/data/orchard.db.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone
from hashlib import sha256

# Force a fresh in-memory DB BEFORE importing the app so settings()
# doesn't latch in a file-backed URL from the env.
os.environ["ORCHARD_ORACLE_DB_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from oracle.app.config import reset_settings_for_tests
from oracle.app.db import Base, get_db, reset_for_tests
from oracle.app.main import app

NODE_ID = "0123456789ABCDEF0123456789ABCDEF"
KEY_HEX = "00112233445566778899AABBCCDDEEFF00112233445566778899AABBCCDDEEFF"


@pytest.fixture()
def client(monkeypatch):
    # Most legacy tests POST /register without an Authorization header.
    # Phase 6.6 made unauthenticated /register a 401 by default. Pin
    # the legacy path explicitly so these tests keep covering their
    # original concern (the registration-vs-Pass-binding logic) and
    # auth-specific behavior is exercised separately via the
    # `auth_register_client` fixture below.
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    reset_settings_for_tests()
    reset_for_tests()

    # StaticPool keeps a single connection alive so all sessions see the
    # same in-memory DB (without it, each connection gets its own empty DB).
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sign(body: bytes) -> str:
    secret = bytes.fromhex(KEY_HEX)
    return hmac.new(secret, body, sha256).hexdigest().upper()


def test_root_identifies_service(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "the-orchard-oracle"
    assert "current_season" in body


def test_register_then_list(client: TestClient):
    r = client.post(
        "/register",
        json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX, "label": "tree-A"},
    )
    assert r.status_code == 201
    assert r.json()["new"] is True

    # Re-register same node + same key is idempotent (200ish, new=False).
    r2 = client.post(
        "/register",
        json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX, "label": "tree-A-renamed"},
    )
    assert r2.status_code == 201
    assert r2.json()["new"] is False

    # Different key on same node_id => 409 conflict.
    r3 = client.post(
        "/register",
        json={"node_id": NODE_ID, "signing_key_hex": "FF" * 32, "label": "imposter"},
    )
    assert r3.status_code == 409

    listing = client.get("/nodes")
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert listing.json()[0]["node_id"] == NODE_ID


def test_post_reading_unknown_node(client: TestClient):
    body = json.dumps({"sensors": {}}).encode("utf-8")
    r = client.post(
        "/readings",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Orchard-Node": NODE_ID,
            "X-Orchard-Sig": _sign(body),
        },
    )
    assert r.status_code == 404


def test_post_reading_bad_signature(client: TestClient):
    # Register the Tree first.
    client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})

    body = json.dumps({"sensors": {}}).encode("utf-8")
    r = client.post(
        "/readings",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Orchard-Node": NODE_ID,
            "X-Orchard-Sig": "00" * 32,  # wrong
        },
    )
    assert r.status_code == 401


def test_post_reading_happy_path_and_retrieve(client: TestClient):
    client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})

    payload = {
        "node_id": NODE_ID,
        "fw": "0.1.0",
        "ts_ms": 12345,
        "sensors": {
            "mq135": {"adc_raw": 1820.0, "voltage_v": 1.46},
            "gps": {"fix": True, "lat": 38.0046, "lon": -85.7374, "satellites": 7},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    r = client.post(
        "/readings",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Orchard-Node": NODE_ID,
            "X-Orchard-Sig": _sign(body),
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["id"] >= 1

    # Reading retrievable.
    r2 = client.get(f"/readings/{NODE_ID}")
    assert r2.status_code == 200
    rows = r2.json()
    assert len(rows) == 1
    assert rows[0]["fw_version"] == "0.1.0"
    assert rows[0]["gps_lat"] == pytest.approx(38.0046)
    assert rows[0]["gps_fix"] is True
    assert rows[0]["payload"]["sensors"]["mq135"]["adc_raw"] == 1820.0

    # DataLayer publisher window: since_ms/until_ms on tree_ts_ms.
    assert len(client.get(f"/readings/{NODE_ID}", params={"since_ms": 12345}).json()) == 1
    assert len(client.get(f"/readings/{NODE_ID}", params={"until_ms": 12345}).json()) == 0
    assert len(client.get(
        f"/readings/{NODE_ID}", params={"since_ms": 12345, "until_ms": 12346}
    ).json()) == 1
    assert len(client.get(f"/readings/{NODE_ID}", params={"since_ms": 99999}).json()) == 0

    # Uptime bucket incremented.
    season = client.get("/").json()["current_season"]
    r3 = client.get(f"/uptime/{NODE_ID}/{season}")
    assert r3.status_code == 200
    assert r3.json()["hours_online"] == 1
    assert len(r3.json()["hour_buckets"]) == 1


def test_uptime_for_unknown_node(client: TestClient):
    r = client.get(f"/uptime/{NODE_ID}/1")
    assert r.status_code == 404


# ---------------- Phase 6.5: Orchard Pass gating ----------------

# Real-looking values pulled from the on-chain Genesis collection so
# anyone reading these tests can see them on MintGarden if they want
# to cross-reference.
PASS_OWNER_ADDR = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
PASS_OWNER_NFT_BECH32 = "nft1n00ugdl737xc6ht4yjdc3cer047lcz9actdxfzpxyat3tsu72z0q46g56z"
NON_OWNER_ADDR = "xch1nobody00000000000000000000000000000000000000000000000000zz0t"


@pytest.fixture()
def fake_indexer(monkeypatch):
    """Stub the on-chain Pass-ownership lookup with a controllable
    in-memory fake. Lets us exercise the /register Pass gate without a
    real MintGarden round-trip — tests stay hermetic.
    """
    from oracle.app import pass_verify
    pass_verify.clear_cache()

    state = {
        PASS_OWNER_ADDR: [{
            "nft_coin_id":    PASS_OWNER_NFT_BECH32,
            "launcher_id":    "f" * 64,
            "name":           "Orchard Pass #0001",
            "edition_number": 1,
            "owner_address":  PASS_OWNER_ADDR,
        }],
        NON_OWNER_ADDR: [],
    }
    err = {"raise": None}

    def fake_list(address: str):
        if err["raise"] is not None:
            from orchard_chia.nft.verify import IndexerError
            raise IndexerError(err["raise"])
        return list(state.get(address, []))

    monkeypatch.setattr(
        "orchard_chia.nft.verify.list_passes_by_address", fake_list)

    yield {
        "state":     state,
        "fail_with": lambda msg: err.__setitem__("raise", msg),
        "succeed":   lambda: err.__setitem__("raise", None),
    }

    pass_verify.clear_cache()


def test_register_without_wallet_skips_pass_gate(client: TestClient, fake_indexer):
    """Legacy registration without a wallet still works and leaves the
    Pass binding null — backward compatible with pre-6.5 nodes."""
    r = client.post(
        "/register",
        json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX, "label": "legacy"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["new"] is True
    assert body["pass_nft_id"] is None
    assert body["pass_verified_at"] is None


def test_register_with_pass_holder_wallet_binds_nft(client: TestClient, fake_indexer):
    """Valid wallet holding a Pass: registration succeeds and the
    bech32 nft_id is bound to the Tree."""
    r = client.post(
        "/register",
        json={
            "node_id":        NODE_ID,
            "signing_key_hex": KEY_HEX,
            "wallet_address": PASS_OWNER_ADDR,
            "label":          "operator-1",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["new"] is True
    assert body["pass_nft_id"] == PASS_OWNER_NFT_BECH32
    assert body["pass_verified_at"] is not None

    # GET /nodes/<id> surfaces the binding.
    r2 = client.get(f"/nodes/{NODE_ID}")
    assert r2.status_code == 200
    assert r2.json()["pass_nft_id"] == PASS_OWNER_NFT_BECH32


def test_register_with_non_holder_wallet_returns_403(client: TestClient, fake_indexer):
    """Wallet that doesn't hold a Pass: registration rejected with 403.
    No partial node row left behind."""
    r = client.post(
        "/register",
        json={
            "node_id":        NODE_ID,
            "signing_key_hex": KEY_HEX,
            "wallet_address": NON_OWNER_ADDR,
        },
    )
    assert r.status_code == 403
    assert "does not hold an Orchard Pass" in r.json()["detail"]

    # No node was created.
    assert client.get(f"/nodes/{NODE_ID}").status_code == 404
    assert client.get("/nodes").json() == []


def test_register_with_malformed_wallet_returns_422(client: TestClient, fake_indexer):
    """Pydantic validator rejects bad xch1 syntax before we ever
    touch the indexer."""
    r = client.post(
        "/register",
        json={
            "node_id":        NODE_ID,
            "signing_key_hex": KEY_HEX,
            "wallet_address": "not-an-xch-address",
        },
    )
    assert r.status_code == 422


def test_register_with_indexer_down_returns_503(client: TestClient, fake_indexer):
    """Indexer error -> 503 Service Unavailable. We refuse to register
    without proof when proof was requested; operator should retry."""
    fake_indexer["fail_with"]("MintGarden 500: bad gateway")
    r = client.post(
        "/register",
        json={
            "node_id":        NODE_ID,
            "signing_key_hex": KEY_HEX,
            "wallet_address": PASS_OWNER_ADDR,
        },
    )
    assert r.status_code == 503
    assert "indexer error" in r.json()["detail"]
    assert client.get(f"/nodes/{NODE_ID}").status_code == 404


def test_reregister_changing_wallet_rebinds_pass(client: TestClient, fake_indexer):
    """Operator initially registered without a wallet, later attaches
    one. Re-register updates wallet_address and binds the Pass."""
    # First register: no wallet.
    client.post(
        "/register",
        json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX},
    )
    # Re-register with the Pass-holding wallet.
    r = client.post(
        "/register",
        json={
            "node_id":        NODE_ID,
            "signing_key_hex": KEY_HEX,
            "wallet_address": PASS_OWNER_ADDR,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["new"] is False
    assert body["pass_nft_id"] == PASS_OWNER_NFT_BECH32


def test_pass_verify_cache_hit(client: TestClient, fake_indexer, monkeypatch):
    """The cache prevents a flapping operator from generating one
    MintGarden call per retry within the TTL window."""
    from oracle.app import pass_verify
    pass_verify.clear_cache()

    calls = {"n": 0}
    original = pass_verify.nft_verify.list_passes_by_address

    def counting(address: str):
        calls["n"] += 1
        return original(address)

    monkeypatch.setattr(
        "orchard_chia.nft.verify.list_passes_by_address", counting)

    # Two registrations of two different nodes from the same wallet.
    for nid in [NODE_ID, "FEDCBA9876543210FEDCBA9876543210"]:
        r = client.post(
            "/register",
            json={
                "node_id":        nid,
                "signing_key_hex": KEY_HEX,
                "wallet_address": PASS_OWNER_ADDR,
            },
        )
        assert r.status_code == 201, r.text
    # Cache hit on the second call.
    assert calls["n"] == 1


# ---------------- Phase 5.5: chain attestation tracking ----------------

def test_record_attestation_unknown_node_404(client: TestClient):
    """POST /attestations rejects unknown node_id to prevent orphan rows."""
    r = client.post("/attestations", json={
        "node_id": "DEADBEEFDEADBEEFDEADBEEFDEADBEEF0",
        "season_number": 2,
        "hours_online": 24,
        "data_hash": "a" * 64,
        "oracle_sig": "b" * 64,
        "dl_tx_id":   "0x" + "c" * 64,
        "dl_key_hex": "61747465737400000",
    })
    assert r.status_code == 404


def test_record_attestation_happy_then_idempotent(client: TestClient):
    """First POST creates, second POST for same (node, season) updates."""
    # Need a node first.
    client.post("/register",
                json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})

    body = {
        "node_id":               NODE_ID,
        "season_number":         3,
        "hours_online":          24,
        "data_hash":             "a" * 64,
        "oracle_sig":            "b" * 64,
        "dl_tx_id":              "0x" + "c" * 64,
        "dl_key_hex":            "61747465737400000",
        "block_height_at_write": 8804917,
    }
    r1 = client.post("/attestations", json=body)
    assert r1.status_code == 201, r1.text
    j1 = r1.json()
    assert j1["season_number"] == 3
    assert j1["dl_tx_id"] == body["dl_tx_id"]
    assert j1["hours_online"] == 24

    # Re-post with new tx_id (re-run scenario) — same row, updated chain pointer.
    body["dl_tx_id"] = "0x" + "d" * 64
    r2 = client.post("/attestations", json=body)
    assert r2.status_code == 201
    j2 = r2.json()
    assert j2["dl_tx_id"] == body["dl_tx_id"]
    # Idempotency: only ONE row exists for (node, season), not two.
    rAll = client.get(f"/attestations/{NODE_ID}")
    assert rAll.status_code == 200
    assert len([row for row in rAll.json() if row["season_number"] == 3]) == 1

    # GET /attestations/<id>/latest returns it.
    rL = client.get(f"/attestations/{NODE_ID}/latest")
    assert rL.status_code == 200
    assert rL.json()["season_number"] == 3


def test_latest_attestation_none_when_empty(client: TestClient):
    """A registered node with no attestations yet returns null."""
    client.post("/register",
                json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    r = client.get(f"/attestations/{NODE_ID}/latest")
    assert r.status_code == 200
    assert r.json() is None


def test_list_attestations_newest_first(client: TestClient):
    client.post("/register",
                json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    for s in [2, 5, 3, 4]:
        client.post("/attestations", json={
            "node_id":      NODE_ID,
            "season_number": s,
            "hours_online":  24,
            "data_hash":     "a" * 64,
            "oracle_sig":    "b" * 64,
            "dl_tx_id":      "0x" + f"{s:064d}",
            "dl_key_hex":    f"00{s:04d}",
        })
    r = client.get(f"/attestations/{NODE_ID}")
    assert r.status_code == 200
    rows = r.json()
    assert [row["season_number"] for row in rows] == [5, 4, 3, 2]


# ---------------- Phase 6.6: wallet auth ----------------

# A real BLS pubkey is 48 bytes. For tests we generate one whose
# corresponding bech32m address we compute ourselves — no Sage/Goby
# needed. The verify endpoint runs in auth_test_mode which skips the
# BLS verify step but still enforces the pk -> address binding check.

class _AuthHarness:
    """Holder so auth_client fixture can yield {client, Session}
    without breaking existing `auth_client.post(...)` test calls.
    .Session is a sessionmaker bound to the same in-memory engine
    the API uses, so tests can do raw DB setup without bypassing
    the dependency override."""
    def __init__(self, client, TestSession):
        self.client = client
        self.Session = TestSession
    def __getattr__(self, name):
        return getattr(self.client, name)


@pytest.fixture()
def auth_client(monkeypatch):
    """Same as `client` but with auth_test_mode=True so the BLS
    signature verify is skipped (we still verify the pk -> address
    binding). Lets us exercise the challenge/verify/whoami flow
    without a real wallet."""
    monkeypatch.setenv("ORCHARD_ORACLE_AUTH_TEST_MODE", "true")
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")
    # Same as for `client` — most auth-flow tests register a Tree via
    # HTTP without holding a session token. Specific register-hardening
    # tests below override this env per-test to exercise the gate.
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")

    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import Base, get_db, reset_for_tests
    from oracle.app import sessions
    from oracle.app.main import app
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    reset_settings_for_tests()
    reset_for_tests()
    sessions.reset_for_tests()

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield _AuthHarness(c, TestSession)
    app.dependency_overrides.clear()
    sessions.reset_for_tests()


def _test_keypair():
    """Construct a (pk_hex, address) pair using our own derivation.
    The pk is just 48 deterministic bytes; the address is whatever
    our puzzle_hash_for_synthetic_pk + bech32m derives. This is
    enough for the pk -> address binding check in test_mode."""
    from oracle.app.wallet_auth import xch_address_for_synthetic_pk
    pk = b"\xab" * 48
    return pk.hex(), xch_address_for_synthetic_pk(pk)


def test_challenge_issues_a_nonce(auth_client: TestClient):
    r = auth_client.post("/auth/challenge")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nonce"]) >= 32
    # The user-facing message must:
    #   - identify which oracle this is (so the operator knows what
    #     they're authorizing in the wallet's sign prompt)
    #   - bind the signature to this specific challenge (the nonce
    #     appearing in the message text is what makes replay obvious)
    # It does NOT need to contain "Chia Signed Message" anymore — that
    # prefix is applied as a CLVM cons-cell wrapper at sign time, not
    # as a user-visible string concatenation. (Including it in the
    # readable text was a leftover bug from when verify did UTF-8
    # concatenation; Sage would have hashed it twice.)
    assert "Orchard View" in body["message"]
    assert body["nonce"] in body["message"]
    assert body["expires_at"] > 0


def test_verify_happy_path_issues_session(auth_client: TestClient):
    pk_hex, addr = _test_keypair()

    # Get a nonce.
    rc = auth_client.post("/auth/challenge")
    assert rc.status_code == 200
    nonce = rc.json()["nonce"]

    # Submit a "signed" challenge. auth_test_mode skips BLS verify
    # but still checks the pk -> address binding.
    rv = auth_client.post("/auth/verify", json={
        "address":    addr,
        "public_key": pk_hex,
        "signature":  "00" * 96,    # bytes ignored in test_mode
        "nonce":      nonce,
    })
    assert rv.status_code == 200, rv.text
    body = rv.json()
    assert body["address"] == addr
    assert len(body["session_token"]) > 50
    token = body["session_token"]

    # whoami round-trip with the token.
    rw = auth_client.get(
        "/auth/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rw.status_code == 200
    assert rw.json()["address"] == addr


def test_verify_rejects_pk_address_mismatch(auth_client: TestClient):
    """Wrong pk for the claimed address must fail even in test_mode.
    This is the critical security check — without it, an attacker
    could submit their own signed challenge under someone else's
    address."""
    _, addr = _test_keypair()
    rc = auth_client.post("/auth/challenge")
    nonce = rc.json()["nonce"]

    bogus_pk = ("cd" * 48)   # different bytes, different derived address
    r = auth_client.post("/auth/verify", json={
        "address":    addr,
        "public_key": bogus_pk,
        "signature":  "00" * 96,
        "nonce":      nonce,
    })
    assert r.status_code == 401
    # Generic message (hardening: don't be a verification oracle); the
    # precise pk-binding reason is logged server-side, not returned.
    assert "verification failed" in r.json()["detail"].lower()


def test_verify_rejects_unknown_nonce(auth_client: TestClient):
    pk_hex, addr = _test_keypair()
    r = auth_client.post("/auth/verify", json={
        "address":    addr,
        "public_key": pk_hex,
        "signature":  "00" * 96,
        "nonce":      "deadbeef" * 8,
    })
    assert r.status_code == 401
    assert "unknown or expired" in r.json()["detail"]


def test_verify_rejects_replay(auth_client: TestClient):
    """Same nonce can be consumed only once."""
    pk_hex, addr = _test_keypair()
    rc = auth_client.post("/auth/challenge")
    nonce = rc.json()["nonce"]

    r1 = auth_client.post("/auth/verify", json={
        "address": addr, "public_key": pk_hex,
        "signature": "00" * 96, "nonce": nonce,
    })
    assert r1.status_code == 200

    r2 = auth_client.post("/auth/verify", json={
        "address": addr, "public_key": pk_hex,
        "signature": "00" * 96, "nonce": nonce,
    })
    assert r2.status_code == 401


def test_whoami_without_token_returns_401(auth_client: TestClient):
    r = auth_client.get("/auth/whoami")
    assert r.status_code == 401


def test_whoami_with_garbage_token_returns_401(auth_client: TestClient):
    r = auth_client.get(
        "/auth/whoami",
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert r.status_code == 401


def test_puzzle_hash_for_synthetic_pk_matches_canonical_chia_spec():
    """Pin the live Sage WalletConnect test vector against the value
    that the official chia library's ``puzzle_for_synthetic_public_key
    (pk).get_tree_hash()`` produces. Without this, any drift in the
    hand-rolled ``curry_and_treehash`` would silently break the pk
    -> address binding check (i.e. all wallet logins would 401 with
    "pk-binding check failed") — exactly the regression that broke
    the live flow before this fix landed.

    Test vector source: a real Sage Wallet chia_signMessageByAddress
    response received during the Phase 6.6 bring-up. The address was
    decoded with chia.util.bech32m.decode_puzzle_hash and the puzzle
    hash with chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle
    .puzzle_for_synthetic_public_key(pk).get_tree_hash() — both inside
    the canonical chia 2.7.1 library."""
    from oracle.app.wallet_auth import (
        puzzle_hash_for_synthetic_pk,
        xch_address_for_synthetic_pk,
    )

    # Synthetic pk returned by Sage for the address below. NB: this
    # is the *synthetic* pk, not the master pk — CHIP-22 wallets
    # return the synthetic key already.
    SAGE_PK = bytes.fromhex(
        "9837de38806397d09c570ec84867e009bd6c39756ffd1ad7d4130f07d2e7a52f"
        "90471324857e03ec6b22752d7f76bb7d"
    )
    EXPECTED_ADDR = (
        "xch1kdzqtkpd42n2avcr6qwdvj69fjn97xl555v0lkpvfg84gdfuchsqee3j04"
    )
    EXPECTED_PH = bytes.fromhex(
        "b34405d82daaa6aeb303d01cd64b454ca65f1bf4a518ffd82c4a0f54353cc5e0"
    )

    assert puzzle_hash_for_synthetic_pk(SAGE_PK) == EXPECTED_PH
    assert xch_address_for_synthetic_pk(SAGE_PK) == EXPECTED_ADDR


def test_parse_signature_message_matches_sage():
    """Mirror Sage's parse_signature_message Rust unit tests verbatim.
    Source: xch-dev/sage/crates/sage/src/utils/parse.rs."""
    from oracle.app.wallet_auth import _parse_signature_message as p

    # hex with 0x prefix
    assert p("0x1234567890abcdef") == bytes.fromhex("1234567890abcdef")
    # hex without prefix
    assert p("1234567890abcdef")   == bytes.fromhex("1234567890abcdef")
    # Plain text (non-hex chars present)
    assert p("Hello, world!")      == b"Hello, world!"
    # Short hex variants
    assert p("0xcafe") == b"\xca\xfe"
    assert p("cafe")   == b"\xca\xfe"
    # Empty string is NOT all-hex (the .is_empty() guard) — UTF-8 path
    assert p("") == b""
    # Mixed-case hex still hex
    assert p("CaFe") == b"\xca\xfe"


def test_signed_message_payload_pins_cons_cell_recipe():
    """Pin the 32-byte tree-hash that Sage feeds into AugScheme.sign.
    Computed by hand from the spec — left=b'Chia Signed Message',
    right=parse_signature_message(msg), then shatree_pair."""
    import hashlib
    from oracle.app.wallet_auth import signed_message_payload

    def sha(b): return hashlib.sha256(b).digest()

    def expected(msg_bytes: bytes) -> bytes:
        left  = sha(b"\x01" + b"Chia Signed Message")
        right = sha(b"\x01" + msg_bytes)
        return sha(b"\x02" + left + right)

    # Plain-text path
    assert signed_message_payload("hello") == expected(b"hello")
    assert signed_message_payload(
        "Sign in to Orchard View.\n\nChallenge nonce: " + "a" * 64
    ) == expected(
        ("Sign in to Orchard View.\n\nChallenge nonce: " + "a" * 64).encode("utf-8")
    )

    # Hex auto-detect path: oracle nonce is always all-hex, so the
    # wallet hex-decodes to raw bytes before tree-hashing.
    assert signed_message_payload("deadbeef") == expected(b"\xde\xad\xbe\xef")
    assert signed_message_payload("0xdeadbeef") == expected(b"\xde\xad\xbe\xef")


def test_verify_chia_signed_message_round_trip_through_real_bls():
    """End-to-end BLS verify with a sk we generate ourselves: sign with
    AugScheme + signed_message_payload, verify through the public API.
    Proves the cons-cell recipe AND the chia-rs glue are wired right.
    Without this, the live wallet sig from Sage will continue to fail
    "BLS signature verification failed" even after the cons-cell fix —
    because there's no way to know that AugScheme is being fed the
    exact same 32 bytes Sage hands to its sign() function."""
    from chia_rs import AugSchemeMPL, PrivateKey
    from oracle.app.wallet_auth import (
        signed_message_payload,
        verify_chia_signed_message,
        xch_address_for_synthetic_pk,
    )

    # Pick a deterministic synth-sk-like value. AugScheme works with
    # ANY 32-byte scalar; the wallet derivation hierarchy is upstream
    # of this point and doesn't change the verification recipe.
    sk = PrivateKey.from_bytes(b"\x42" * 32)
    pk_bytes = bytes(sk.get_g1())

    addr = xch_address_for_synthetic_pk(pk_bytes)
    message = (
        "Sign in to Orchard View.\n\n"
        f"Challenge nonce: {'7e57'*16}\n"
        "If you didn't initiate this, decline."
    )

    payload = signed_message_payload(message)
    sig_bytes = bytes(AugSchemeMPL.sign(sk, payload))

    result = verify_chia_signed_message(
        address=addr,
        public_key_hex=pk_bytes.hex(),
        signature_hex=sig_bytes.hex(),
        message=message,
    )
    assert result.ok, result.reason

    # Negative control: a one-bit flip in the message must fail the
    # verify. Catches a regression where signed_message_payload
    # accidentally ignored its input.
    bad = verify_chia_signed_message(
        address=addr,
        public_key_hex=pk_bytes.hex(),
        signature_hex=sig_bytes.hex(),
        message=message + "x",
    )
    assert not bad.ok
    assert "BLS signature verification failed" in bad.reason


# ---------------- Phase 6.6 #52: /network/stats ----------------

def test_network_stats_empty_oracle_returns_zeros(client: TestClient):
    """Fresh oracle with no Trees, readings, or attestations returns
    a well-formed snapshot with zeros — not 500, not 404."""
    r = client.get("/network/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["trees_registered"] == 0
    assert body["trees_active_24h"] == 0
    assert body["readings_total"] == 0
    assert body["readings_last_24h"] == 0
    assert body["attestations_total"] == 0
    assert isinstance(body["current_season"], int)
    assert body["as_of_utc"].startswith("20")  # ISO timestamp


def test_network_stats_counts_after_register_and_reading(client: TestClient):
    """After a Tree registers + posts a reading, the stats reflect it."""
    nid = "AB" * 16
    skey = "11" * 32
    rr = client.post("/register", json={"node_id": nid, "signing_key_hex": skey})
    assert rr.status_code == 201

    # Post one signed reading. Use the canonical X-Orchard-* headers
    # the firmware uses.
    body = json.dumps({"node_id": nid, "ts_ms": 1, "sensors": {}}).encode()
    sig = hmac.new(bytes.fromhex(skey), body, sha256).hexdigest().upper()
    pr = client.post("/readings", content=body,
                     headers={"Content-Type": "application/json",
                              "X-Orchard-Node": nid,
                              "X-Orchard-Sig": sig})
    assert pr.status_code == 202, pr.text

    r = client.get("/network/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["trees_registered"] == 1
    assert body["trees_active_24h"] == 1
    assert body["readings_total"] == 1
    assert body["readings_last_24h"] == 1


def test_network_stats_does_not_expose_per_tree_data(client: TestClient):
    """Belt-and-braces: register a Tree with a wallet, then assert
    that the wallet address never appears in /network/stats. This is
    the privacy contract of the endpoint."""
    addr = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
    nid = "CD" * 16
    client.post("/register", json={
        "node_id": nid, "signing_key_hex": "22" * 32,
        "wallet_address": addr,
    })
    # Pass check may 403 against a fake wallet; either way the
    # subsequent stats call must NEVER include the address.
    r = client.get("/network/stats")
    assert r.status_code == 200
    text = r.text
    assert addr not in text
    assert nid not in text


# ---------------- Phase 6.6: /register hardening ----------------
#
# The new policy: /register requires Authorization: Bearer <token>
# from a wallet that completed /auth/challenge + /auth/verify. The
# resolved session.address is the authoritative wallet identity for
# Pass binding and the Node row; body.wallet_address is either ignored
# or, if present and mismatched, rejected with 400.


@pytest.fixture()
def gated_register_client(monkeypatch):
    """Like `auth_client` but with require_wallet_session forced ON,
    so we can verify the gate actually rejects unauthenticated and
    cross-wallet attempts."""
    monkeypatch.setenv("ORCHARD_ORACLE_AUTH_TEST_MODE", "true")
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "true")
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")

    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import Base, get_db, reset_for_tests
    from oracle.app import sessions
    from oracle.app.main import app

    reset_settings_for_tests()
    reset_for_tests()
    sessions.reset_for_tests()

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    sessions.reset_for_tests()


def _bearer_for(client: TestClient) -> tuple[str, str]:
    """Run a full challenge -> verify cycle (in test_mode) and return
    (bearer_token, address). Uses our own derivation so the BLS step
    is bypassed but the pk -> address binding is still real."""
    pk_hex, addr = _test_keypair()
    rc = client.post("/auth/challenge")
    nonce = rc.json()["nonce"]
    rv = client.post("/auth/verify", json={
        "address":    addr,
        "public_key": pk_hex,
        "signature":  "00" * 96,
        "nonce":      nonce,
    })
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"], addr


def test_register_without_session_returns_401(gated_register_client: TestClient):
    """The whole point of the hardening — anonymous /register is gone."""
    r = gated_register_client.post("/register", json={
        "node_id": "AA" * 16, "signing_key_hex": "11" * 32,
    })
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "wallet session" in detail or "WalletConnect" in detail


def test_register_with_session_uses_session_address_not_body(
    monkeypatch, gated_register_client: TestClient
):
    """The session's verified address is the source of truth — the
    body's wallet_address is ignored when present and matching, and
    a missing body wallet_address is fine (the session supplies it).
    Pass-check is monkeypatched to return a sentinel so we can prove
    the right wallet went into the check."""
    from oracle.app.routes import register as register_mod

    seen_wallets = []
    def fake_first_pass(addr):
        seen_wallets.append(addr)
        return "nft1FAKE"
    monkeypatch.setattr(register_mod.pass_verify, "first_pass_nft_id", fake_first_pass)
    monkeypatch.setattr(register_mod.pass_verify, "utcnow",
                        lambda: datetime(2026, 6, 1, tzinfo=timezone.utc))

    token, addr = _bearer_for(gated_register_client)

    # Case A: body omits wallet_address entirely.
    r = gated_register_client.post(
        "/register",
        json={"node_id": "AA" * 16, "signing_key_hex": "11" * 32},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["pass_nft_id"] == "nft1FAKE"
    assert seen_wallets == [addr]


def test_register_rejects_mismatched_body_wallet_address(
    monkeypatch, gated_register_client: TestClient
):
    """Body wallet_address that doesn't match the session is a 400 —
    a confused or malicious client. We surface it rather than silently
    overriding because silent override would hide bugs in the wizard."""
    from oracle.app.routes import register as register_mod
    monkeypatch.setattr(register_mod.pass_verify, "first_pass_nft_id",
                        lambda addr: "nft1IGNORED")

    token, addr = _bearer_for(gated_register_client)
    other_wallet = (
        "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqs0nfx5p"
    )
    assert other_wallet != addr

    r = gated_register_client.post(
        "/register",
        json={
            "node_id": "BB" * 16, "signing_key_hex": "22" * 32,
            "wallet_address": other_wallet,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]


def test_register_accepts_matching_body_wallet_address(
    monkeypatch, gated_register_client: TestClient
):
    """If the body redundantly carries the same wallet as the session,
    that's harmless — accept it. (The wizard could simplify and drop
    the field; older clients can keep sending it during transition.)"""
    from oracle.app.routes import register as register_mod
    monkeypatch.setattr(register_mod.pass_verify, "first_pass_nft_id",
                        lambda addr: "nft1MATCH")
    monkeypatch.setattr(register_mod.pass_verify, "utcnow",
                        lambda: datetime(2026, 6, 1, tzinfo=timezone.utc))

    token, addr = _bearer_for(gated_register_client)

    r = gated_register_client.post(
        "/register",
        json={
            "node_id": "CC" * 16, "signing_key_hex": "33" * 32,
            "wallet_address": addr,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["pass_nft_id"] == "nft1MATCH"


# ---------------- Phase 6.6: scoped GETs + DELETE ----------------

def test_list_nodes_unauthenticated_returns_all(auth_client: TestClient):
    """Public dashboard behavior — no Authorization header => all nodes."""
    auth_client.post("/register", json={"node_id": "AA" * 16, "signing_key_hex": "11" * 32})
    auth_client.post("/register", json={"node_id": "BB" * 16, "signing_key_hex": "22" * 32})
    r = auth_client.get("/nodes")
    assert r.status_code == 200
    ids = {n["node_id"] for n in r.json()}
    assert ids == {"AA" * 16, "BB" * 16}


def test_list_nodes_authenticated_scoped_to_session_wallet(auth_client):
    """With a session bound to wallet X, /nodes returns only nodes
    whose wallet_address == X."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from sqlalchemy import update

    _, addr = _test_keypair()

    auth_client.post("/register", json={
        "node_id": "AA" * 16, "signing_key_hex": "11" * 32,
    })
    auth_client.post("/register", json={
        "node_id": "BB" * 16, "signing_key_hex": "22" * 32,
    })

    # Mint a session directly — the wallet-verify flow is exercised
    # in the dedicated /auth tests; here we just want to test scoping.
    token, _ = sm.issue(addr)

    # Hand-set AA's wallet to addr, BB's wallet to someone else.
    # Use the FIXTURE's sessionmaker so we hit the same in-memory
    # engine the route's get_db dependency uses — `oracle.app.db.
    # session_factory()` would build its own engine and miss the data.
    with auth_client.Session() as s:
        s.execute(update(models.Node).where(
            models.Node.node_id == "AA" * 16).values(wallet_address=addr))
        s.execute(update(models.Node).where(
            models.Node.node_id == "BB" * 16).values(
                wallet_address="xch1otherop00000000000000000000000000000000000000000000000lzckhk"))
        s.commit()

    r = auth_client.get("/nodes", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    ids = [n["node_id"] for n in r.json()]
    assert ids == ["AA" * 16]


def test_get_node_authed_wrong_owner_returns_404(auth_client):
    """A logged-in operator probing for someone else's node_id gets
    404, not 403 — don't leak existence."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from sqlalchemy import update

    _, addr = _test_keypair()
    token, _ = sm.issue(addr)

    auth_client.post("/register", json={
        "node_id": "CC" * 16, "signing_key_hex": "33" * 32,
    })

    with auth_client.Session() as s:
        s.execute(update(models.Node).where(
            models.Node.node_id == "CC" * 16).values(
                wallet_address="xch1otherop00000000000000000000000000000000000000000000000lzckhk"))
        s.commit()

    r = auth_client.get(
        f"/nodes/{'CC' * 16}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    # Without auth, the same id IS retrievable — confirms scoping is
    # the only thing hiding it (vs. the row being missing entirely).
    r2 = auth_client.get(f"/nodes/{'CC' * 16}")
    assert r2.status_code == 200


def test_delete_node_unauthenticated_returns_401(auth_client: TestClient):
    auth_client.post("/register", json={
        "node_id": "DD" * 16, "signing_key_hex": "44" * 32,
    })
    r = auth_client.delete(f"/nodes/{'DD' * 16}")
    assert r.status_code == 401


def test_delete_node_owner_succeeds_cascade(auth_client):
    """Owner-only DELETE removes the node + its child rows."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from sqlalchemy import select, update

    _, addr = _test_keypair()
    token, _ = sm.issue(addr)

    NID = "EE" * 16
    auth_client.post("/register", json={
        "node_id": NID, "signing_key_hex": "55" * 32,
    })
    # Set ownership + add a child row through the fixture's engine.
    with auth_client.Session() as s:
        s.execute(update(models.Node).where(
            models.Node.node_id == NID).values(wallet_address=addr))
        s.add(models.Reading(
            node_id=NID, received_at=datetime.now(timezone.utc),
            payload_json="{}", sig_hex="00" * 32))
        s.commit()

    r = auth_client.delete(
        f"/nodes/{NID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204

    # Gone end-to-end.
    assert auth_client.get(f"/nodes/{NID}").status_code == 404
    with auth_client.Session() as s:
        n_left = s.execute(
            select(models.Reading).where(models.Reading.node_id == NID)
        ).scalars().all()
        assert n_left == []


def test_delete_node_non_owner_returns_404(auth_client):
    """Trying to delete someone else's node returns 404 (not 403)."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from sqlalchemy import update

    _, addr = _test_keypair()
    token, _ = sm.issue(addr)

    NID = "FF" * 16
    auth_client.post("/register", json={
        "node_id": NID, "signing_key_hex": "66" * 32,
    })
    with auth_client.Session() as s:
        s.execute(update(models.Node).where(
            models.Node.node_id == NID).values(
                wallet_address="xch1otherop00000000000000000000000000000000000000000000000lzckhk"))
        s.commit()

    r = auth_client.delete(
        f"/nodes/{NID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    # Confirm node still exists (wasn't accidentally deleted).
    assert auth_client.get(f"/nodes/{NID}").status_code == 200


# ---------------- 2026-06-09 security hardening ----------------

def test_reading_replay_is_deduped(client: TestClient):
    """H2: an exact replay of a signed reading is dropped — same id
    returned, no second row, no uptime double-count."""
    client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    payload = {"node_id": NODE_ID, "ts_ms": 555, "sensors": {}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    h = {"Content-Type": "application/json", "X-Orchard-Node": NODE_ID, "X-Orchard-Sig": _sign(body)}

    r1 = client.post("/readings", content=body, headers=h)
    assert r1.status_code == 202
    first_id = r1.json()["id"]

    r2 = client.post("/readings", content=body, headers=h)  # identical => replay
    assert r2.status_code == 202
    assert r2.json().get("duplicate") is True
    assert r2.json()["id"] == first_id

    assert len(client.get(f"/readings/{NODE_ID}").json()) == 1
    season = client.get("/").json()["current_season"]
    assert client.get(f"/uptime/{NODE_ID}/{season}").json()["hours_online"] == 1


def test_readings_gps_hidden_from_non_owner(auth_client):
    """M1: precise GPS on a wallet-bound node is returned only to the
    owner's session — anonymous/LAN callers get fix/sats but not coords."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from sqlalchemy import update

    _, addr = _test_keypair()
    auth_client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    with auth_client.Session() as s:
        s.execute(update(models.Node).where(
            models.Node.node_id == NODE_ID).values(wallet_address=addr))
        s.commit()

    payload = {"node_id": NODE_ID, "ts_ms": 99,
               "sensors": {"gps": {"fix": True, "lat": 38.0046, "lon": -85.7374, "satellites": 7}}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    pr = auth_client.post("/readings", content=body, headers={
        "Content-Type": "application/json", "X-Orchard-Node": NODE_ID, "X-Orchard-Sig": _sign(body)})
    assert pr.status_code == 202, pr.text

    # Anonymous: coords scrubbed, fix/sats kept.
    row = auth_client.get(f"/readings/{NODE_ID}").json()[0]
    assert row["gps_lat"] is None and row["gps_lon"] is None
    assert row["gps_fix"] is True
    gps = row["payload"]["sensors"]["gps"]
    assert "lat" not in gps and "lon" not in gps
    assert gps["fix"] is True and gps["satellites"] == 7

    # Owner session: coords visible.
    token, _ = sm.issue(addr)
    row2 = auth_client.get(
        f"/readings/{NODE_ID}", headers={"Authorization": f"Bearer {token}"}).json()[0]
    assert row2["gps_lat"] == pytest.approx(38.0046)
    assert row2["payload"]["sensors"]["gps"]["lat"] == pytest.approx(38.0046)


def test_geohash_encode_known_vector():
    """Coarse-location encoder is correct (canonical geohash example) and
    rejects out-of-range coordinates."""
    from oracle.app.routes.nodes import _geohash_encode

    assert _geohash_encode(57.64911, 10.40744, 5) == "u4pru"
    assert _geohash_encode(38.0046, -85.7374, 5) is not None
    assert _geohash_encode(999.0, 0.0, 5) is None


def test_nodes_public_geohash_sensors_and_wallet_scrub(auth_client):
    """worldview globe contract: /nodes exposes a COARSE ~5 km geohash and
    the node's sensor classes to everyone, and scrubs wallet_address to null
    for the public (returned only to the owning session). The Pass binding
    stays public (it's an on-chain credential)."""
    import oracle.app.sessions as sm
    from oracle.app import models
    from oracle.app.routes.nodes import _geohash_encode
    from sqlalchemy import update

    _, addr = _test_keypair()
    auth_client.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    with auth_client.Session() as s:
        s.execute(update(models.Node).where(models.Node.node_id == NODE_ID).values(
            wallet_address=addr, pass_nft_id="nft1testpass0000"))
        s.commit()

    payload = {"node_id": NODE_ID, "ts_ms": 1, "sensors": {
        "mq135": {"adc_raw": 1820.0},
        "gps": {"fix": True, "lat": 38.0046, "lon": -85.7374, "satellites": 7},
    }}
    body = json.dumps(payload, separators=(",", ":")).encode()
    pr = auth_client.post("/readings", content=body, headers={
        "Content-Type": "application/json", "X-Orchard-Node": NODE_ID,
        "X-Orchard-Sig": _sign(body)})
    assert pr.status_code == 202, pr.text

    # Public / the globe (no session): coarse geohash + sensors present;
    # wallet + Pass scrubbed to null.
    pub = auth_client.get(f"/nodes/{NODE_ID}").json()
    assert pub["geohash"] == _geohash_encode(38.0046, -85.7374, 5)
    assert pub["geohash"] is not None and len(pub["geohash"]) == 5
    assert pub["sensors"] == ["gps", "mq135"]
    assert pub["wallet_address"] is None              # wallet scrubbed for public
    assert pub["pass_nft_id"] == "nft1testpass0000"   # Pass binding stays public
    # The list view is scrubbed for the public too.
    lst = auth_client.get("/nodes").json()
    assert lst[0]["wallet_address"] is None
    assert lst[0]["geohash"] == pub["geohash"]

    # Owner session: wallet + Pass visible again; coarse geohash unchanged.
    token, _ = sm.issue(addr)
    own = auth_client.get(f"/nodes/{NODE_ID}",
                          headers={"Authorization": f"Bearer {token}"}).json()
    assert own["wallet_address"] == addr
    assert own["pass_nft_id"] == "nft1testpass0000"
    assert own["geohash"] == pub["geohash"]


def test_fixed_window_limiter():
    """M2: the rate limiter blocks over the limit and resets per window."""
    from oracle.app.ratelimit import FixedWindowLimiter

    t = {"now": 100.0}
    lm = FixedWindowLimiter(limit=3, window_s=60, clock=lambda: t["now"])
    assert [lm.allow("ip") for _ in range(3)] == [True, True, True]
    assert lm.allow("ip") is False        # 4th in-window blocked
    assert lm.allow("other") is True      # independent key
    t["now"] = 161.0                       # window elapsed
    assert lm.allow("ip") is True
    assert FixedWindowLimiter(limit=0).allow("x") is True  # 0 disables


@pytest.fixture()
def writer_token_client(monkeypatch):
    """Client with ORCHARD_ORACLE_WRITER_TOKEN configured, to exercise
    the H1 /attestations auth gate."""
    monkeypatch.setenv("ORCHARD_ORACLE_REQUIRE_WALLET_SESSION", "false")
    monkeypatch.setenv("ORCHARD_ORACLE_WRITER_TOKEN", "testsecret")
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", "sqlite:///:memory:")
    reset_settings_for_tests()
    reset_for_tests()

    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(test_engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_attestations_require_writer_token(writer_token_client: TestClient):
    """H1: with a writer token configured, /attestations POST requires a
    matching X-Orchard-Writer-Token — even from loopback."""
    c = writer_token_client
    c.post("/register", json={"node_id": NODE_ID, "signing_key_hex": KEY_HEX})
    body = {
        "node_id": NODE_ID, "season_number": 2, "hours_online": 24,
        "data_hash": "a" * 64, "oracle_sig": "b" * 64,
        "dl_tx_id": "0x" + "c" * 64, "dl_key_hex": "61747465737400000",
    }
    # No token -> 401.
    assert c.post("/attestations", json=body).status_code == 401
    # Wrong token -> 401.
    assert c.post("/attestations", json=body,
                  headers={"X-Orchard-Writer-Token": "nope"}).status_code == 401
    # Correct token -> 201.
    assert c.post("/attestations", json=body,
                  headers={"X-Orchard-Writer-Token": "testsecret"}).status_code == 201

def test_register_device_pubkey_and_nodes_expose(client: TestClient):
    """ADR-0003: device_pubkey is stored at register and public on /nodes."""
    pub = "02" + "ab" * 32
    r = client.post(
        "/register",
        json={
            "node_id": NODE_ID,
            "signing_key_hex": KEY_HEX,
            "device_pubkey": pub,
        },
    )
    assert r.status_code == 201, r.text
    nodes = client.get("/nodes").json()
    assert nodes[0]["device_pubkey"] == pub
    one = client.get(f"/nodes/{NODE_ID}").json()
    assert one["device_pubkey"] == pub

    # Same key re-register ok; different key conflicts.
    ok = client.post(
        "/register",
        json={
            "node_id": NODE_ID,
            "signing_key_hex": KEY_HEX,
            "device_pubkey": pub,
        },
    )
    assert ok.status_code == 201
    bad = client.post(
        "/register",
        json={
            "node_id": NODE_ID,
            "signing_key_hex": KEY_HEX,
            "device_pubkey": "03" + "cd" * 32,
        },
    )
    assert bad.status_code == 409


def test_beacon_placeholder_and_env(client: TestClient, monkeypatch):
    import oracle.app.routes.beacon as be
    be._cache = None
    be._cache_mono = 0.0
    monkeypatch.delenv("ORCHARD_BEACON_BLOCK_ANCHOR", raising=False)
    monkeypatch.delenv("ORCHARD_BEACON_BLOCK_HEIGHT", raising=False)
    r = client.get("/beacon")
    assert r.status_code == 200
    body = r.json()
    assert body["block_anchor"] == "0" * 16
    assert body["ok"] is False

    be._cache = None
    monkeypatch.setenv("ORCHARD_BEACON_BLOCK_ANCHOR", "a1b2c3d4e5f6071899")
    monkeypatch.setenv("ORCHARD_BEACON_BLOCK_HEIGHT", "42")
    r2 = client.get("/beacon")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["ok"] is True
    assert b2["block_anchor"] == "a1b2c3d4e5f60718"
    assert b2["block_height"] == 42
    assert b2["source"] == "env"


def test_register_rejects_bad_device_pubkey(client: TestClient):
    r = client.post(
        "/register",
        json={
            "node_id": NODE_ID,
            "signing_key_hex": KEY_HEX,
            "device_pubkey": "01" + "aa" * 32,  # bad prefix
        },
    )
    assert r.status_code == 422

def test_beacon_cache_hits_second_call(client, monkeypatch):
    """Second /beacon within TTL should set cached=true without re-load."""
    monkeypatch.setenv("ORCHARD_BEACON_BLOCK_ANCHOR", "a1b2c3d4e5f6071899")
    monkeypatch.setenv("ORCHARD_BEACON_BLOCK_HEIGHT", "9")
    # Reset module cache
    import oracle.app.routes.beacon as be
    be._cache = None
    be._cache_mono = 0.0
    be._CACHE_TTL_S = 60.0
    r1 = client.get("/beacon")
    assert r1.status_code == 200
    assert r1.json()["cached"] is False
    r2 = client.get("/beacon")
    assert r2.json()["cached"] is True
    assert r2.json()["block_anchor"] == "a1b2c3d4e5f60718"
