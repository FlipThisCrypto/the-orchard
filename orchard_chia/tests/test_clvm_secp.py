# SPDX-License-Identifier: Apache-2.0
"""ADR-0007 cross-check (b): device signatures verify under Chia's VM.

Runs CLVM's ``secp256r1_verify`` operator — via ``chia_rs.run_chia_program``,
the same clvm_rs the mainnet consensus uses (chia-dev-tools' simulator wraps
this very VM; we call it at the operator level, full coin-spend simulation
arrives with the puzzle toolchain, HANDOVER T19/T20) — against the exact
signatures our schema produces. This is the proof that the on-chain door is
open: a ChiaLisp puzzle can check a Tree's reading signature directly
(ADR-0008 heartbeats depend on it).

The operator consumes exactly our SPEC §4.1 encodings:

    (secp256r1_verify <pubkey 33B compressed SEC1> <sha256 digest 32B> <sig 64B r||s>)

returning nil on success and RAISING on failure.

Trap pinned by the tamper tests: CLVM treats an *unknown* operator atom as a
costed no-op that "succeeds" on anything. A wrong opcode therefore yields a
test that always passes. The tampered/wrong-key rejections below are what
prove the real verifier ran.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from chia_rs import run_chia_program

from orchard_chia.datalayer import schema

VEC = json.loads(
    (Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json")
    .read_text(encoding="utf-8")
)

# CHIP-0011 operator, core since the CHIP-0012 hard fork. Probed against
# chia_rs: good sig -> nil at cost ~1.85M; bad sig -> ValueError.
SECP256R1_VERIFY_OP = bytes.fromhex("1c3a8f00")
MAX_COST = 11_000_000_000


# --- minimal CLVM serializer (atoms + proper lists only) -------------------- #
def _atom(b: bytes) -> bytes:
    n = len(b)
    if n == 0:
        return b"\x80"
    if n == 1 and b[0] <= 0x7F:
        return b
    if n <= 0x3F:
        return bytes([0x80 | n]) + b
    if n <= 0x1FFF:
        return bytes([0xC0 | (n >> 8), n & 0xFF]) + b
    raise ValueError("atom too long for this serializer")


def _quoted(b: bytes) -> bytes:
    """(q . <atom>) — 0xff pair, opcode 1, then the atom."""
    return b"\xff\x01" + _atom(b)


def _verify_program(pubkey: bytes, digest: bytes, sig: bytes) -> bytes:
    """Serialized `(secp256r1_verify (q . pk) (q . digest) (q . sig))`."""
    out = b"\xff" + _atom(SECP256R1_VERIFY_OP)
    for arg in (pubkey, digest, sig):
        out += b"\xff" + _quoted(arg)
    return out + b"\x80"


def clvm_secp256r1_verify(pubkey: bytes, digest: bytes, sig: bytes) -> int:
    """Run the operator under the consensus VM; return cost. Raises on
    invalid signature (ValueError from clvm_rs)."""
    cost, _ = run_chia_program(
        _verify_program(pubkey, digest, sig), b"\x80", MAX_COST, 0
    )
    return cost


def _digest_of(signed_record: dict, sig_field: str = "sig") -> bytes:
    body = {k: v for k, v in signed_record.items() if k != sig_field}
    return hashlib.sha256(schema.canonical_bytes(body)).digest()


# --- the cross-check -------------------------------------------------------- #
def test_every_vector_reading_sig_verifies_on_chain():
    pk = bytes.fromhex(VEC["device"]["pubkey"])
    for stored in VEC["readings_signed"]:
        cost = clvm_secp256r1_verify(pk, _digest_of(stored), bytes.fromhex(stored["sig"]))
        assert cost > 0


def test_oracle_season_sig_verifies_on_chain():
    # Same curve everywhere: the Season attest signature is chain-checkable too.
    attest = VEC["records"]["attest"]
    pk = bytes.fromhex(VEC["oracle"]["pubkey"])
    assert clvm_secp256r1_verify(
        pk, _digest_of(attest, "oracle_sig"), bytes.fromhex(attest["oracle_sig"])
    ) > 0


def test_freshly_signed_reading_verifies_on_chain():
    # Not just the committed vectors: the live signer's output is accepted by
    # the consensus VM end-to-end (sign here, verify "on chain").
    body = {"node_id": "AB" * 16, "ts_ms": 1765500000000,
            "metrics": {"temperature_mc": 20000}}
    signed = schema.sign_reading(body, VEC["device"]["seed"])
    pk = bytes.fromhex(schema.pubkey_for_seed(VEC["device"]["seed"]))
    assert clvm_secp256r1_verify(pk, _digest_of(signed), bytes.fromhex(signed["sig"])) > 0


def test_clvm_rejects_tampered_signature():
    stored = VEC["readings_signed"][0]
    pk = bytes.fromhex(VEC["device"]["pubkey"])
    sig = bytearray(bytes.fromhex(stored["sig"]))
    sig[0] ^= 0x01
    with pytest.raises(ValueError):
        clvm_secp256r1_verify(pk, _digest_of(stored), bytes(sig))


def test_clvm_rejects_tampered_payload():
    stored = dict(VEC["readings_signed"][0])
    pk = bytes.fromhex(VEC["device"]["pubkey"])
    sig = bytes.fromhex(stored["sig"])
    tampered = {**stored, "ts_ms": stored["ts_ms"] + 1}
    with pytest.raises(ValueError):
        clvm_secp256r1_verify(pk, _digest_of(tampered), sig)


def test_clvm_rejects_wrong_pubkey():
    stored = VEC["readings_signed"][0]
    wrong_pk = bytes.fromhex(VEC["oracle"]["pubkey"])
    with pytest.raises(ValueError):
        clvm_secp256r1_verify(wrong_pk, _digest_of(stored), bytes.fromhex(stored["sig"]))
