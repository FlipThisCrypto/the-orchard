# SPDX-License-Identifier: Apache-2.0
"""HANDOVER T20 — the Tree-singleton heartbeat puzzle, under the consensus VM.

Runs the compiled ``puzzles/src/tree_heartbeat.clsp`` (loaded from the pinned
``puzzles/hashes.json``) with ``chia_rs.run_chia_program`` — the same clvm_rs
mainnet consensus uses — driving it with signatures from the live ``schema``
signer. This proves the on-chain half of ADR-0008 §1: a heartbeat spend is

  * authorized only by the Tree's own secp256r1 key (``secp256r1_verify``),
  * rate-limited by a relative timelock (ASSERT_SECONDS_RELATIVE), and
  * bound, via the signature, to the exact state transition + next coin, so a
    captured signature can't be replayed into a different counter/epoch/batch
    or redirected to a coin the Tree didn't authorize.

The puzzle is the singleton INNER puzzle; its identity + current state
(TREE_PUBKEY, MIN_INTERVAL, COUNTER, EPOCH) are curried in at deploy. We test
the uncurried mod with all parameters supplied in the solution — currying is
mechanical and doesn't change the logic exercised here.

Like ``test_clvm_secp.py`` this hand-serializes CLVM (no ``clvm`` dependency):
an *unknown* operator is a costed no-op that "succeeds", so the rejection cases
are what prove the real verifier ran.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from chia_rs import run_chia_program

from orchard_chia.datalayer import schema

REPO_ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_HEX = json.loads(
    (REPO_ROOT / "puzzles" / "hashes.json").read_text(encoding="utf-8")
)["tree_heartbeat"]["clvm_hex"]
PROGRAM = bytes.fromhex(HEARTBEAT_HEX)
MAX_COST = 11_000_000_000

# condition opcodes the puzzle emits
ASSERT_SECONDS_RELATIVE = 80
CREATE_COIN = 51
CREATE_COIN_ANNOUNCEMENT = 60
HB_TAG = 0x6862  # "hb" domain tag, matches tree_heartbeat.clsp


# --- minimal CLVM serializer (atoms, signed-minimal ints, proper lists) ----- #
def _int_to_atom(n: int) -> bytes:
    """CLVM canonical signed-minimal big-endian encoding of a non-negative int.
    This is the exact atom *content* CLVM's ``sha256`` concatenates."""
    assert n >= 0
    if n == 0:
        return b""
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return (b"\x00" + b) if (b[0] & 0x80) else b  # leading 0x00 keeps it positive


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


def _ser(obj) -> bytes:
    """Serialize a Python value to a CLVM blob: list -> proper list, int ->
    signed-minimal atom, bytes -> atom."""
    if isinstance(obj, list):
        out = b"".join(b"\xff" + _ser(item) for item in obj)
        return out + b"\x80"
    if isinstance(obj, int):
        return _atom(_int_to_atom(obj))
    if isinstance(obj, (bytes, bytearray)):
        return _atom(bytes(obj))
    raise TypeError(type(obj))


# --- helpers ---------------------------------------------------------------- #
def _heartbeat_digest(counter: int, epoch: int, batch: bytes, next_ph: bytes) -> bytes:
    # mirrors (sha256 COUNTER EPOCH sensor_batch_hash next_puzzle_hash)
    return hashlib.sha256(
        _int_to_atom(counter) + _int_to_atom(epoch) + batch + next_ph
    ).digest()


def _run(pk, min_interval, counter, epoch, batch, next_ph, sig):
    solution = _ser([pk, min_interval, counter, epoch, batch, next_ph, sig])
    return run_chia_program(PROGRAM, solution, MAX_COST, 0)


def _conditions(out) -> list[list[bytes]]:
    def to_list(s):
        items = []
        while s.pair:
            items.append(s.pair[0])
            s = s.pair[1]
        return items

    return [[atom.atom for atom in to_list(cond)] for cond in to_list(out)]


def _signed_case(min_interval=600, counter=7, epoch=3):
    seed = schema.generate_seed()
    pk = bytes.fromhex(schema.pubkey_for_seed(seed))
    batch = hashlib.sha256(b"sensor-batch-A").digest()
    next_ph = hashlib.sha256(b"next-singleton-coin").digest()
    sig = bytes.fromhex(
        schema.sign_digest(_heartbeat_digest(counter, epoch, batch, next_ph), seed)
    )
    return dict(pk=pk, min_interval=min_interval, counter=counter, epoch=epoch,
                batch=batch, next_ph=next_ph, sig=sig)


# --- the happy path --------------------------------------------------------- #
def test_valid_heartbeat_emits_timelock_recreate_and_announcement():
    c = _signed_case()
    _cost, out = _run(c["pk"], c["min_interval"], c["counter"], c["epoch"],
                      c["batch"], c["next_ph"], c["sig"])
    conds = _conditions(out)

    # 1) relative timelock = MIN_INTERVAL
    assert conds[0][0] == bytes([ASSERT_SECONDS_RELATIVE])
    assert int.from_bytes(conds[0][1], "big") == c["min_interval"]
    # 2) recreate the singleton at the Tree-authorized next coin, amount 1 (odd)
    assert conds[1][0] == bytes([CREATE_COIN])
    assert conds[1][1] == c["next_ph"]
    assert conds[1][2] == bytes([1])
    # 3) announce the heartbeat with the INCREMENTED counter, domain-tagged
    assert conds[2][0] == bytes([CREATE_COIN_ANNOUNCEMENT])
    expected = hashlib.sha256(
        _int_to_atom(HB_TAG) + _int_to_atom(c["counter"] + 1)
        + _int_to_atom(c["epoch"]) + c["batch"]
    ).digest()
    assert conds[2][1] == expected


def test_counter_zero_heartbeat_is_valid():
    # first heartbeat (COUNTER 0) — exercises the empty-atom encoding of 0.
    c = _signed_case(counter=0)
    _cost, out = _run(c["pk"], c["min_interval"], 0, c["epoch"],
                      c["batch"], c["next_ph"], c["sig"])
    assert _conditions(out)[1][1] == c["next_ph"]


# --- rejections (these prove the real secp256r1_verify ran) ----------------- #
def test_rejects_tampered_signature():
    c = _signed_case()
    bad = bytes([c["sig"][0] ^ 0x01]) + c["sig"][1:]
    with pytest.raises(ValueError):
        _run(c["pk"], c["min_interval"], c["counter"], c["epoch"],
             c["batch"], c["next_ph"], bad)


def test_rejects_wrong_pubkey():
    c = _signed_case()
    wrong_pk = bytes.fromhex(schema.pubkey_for_seed(schema.generate_seed()))
    with pytest.raises(ValueError):
        _run(wrong_pk, c["min_interval"], c["counter"], c["epoch"],
             c["batch"], c["next_ph"], c["sig"])


def test_rejects_redirected_next_coin():
    # a relayer can't point the recreation at a coin the Tree didn't sign.
    c = _signed_case()
    thief = hashlib.sha256(b"attacker-controlled-coin").digest()
    with pytest.raises(ValueError):
        _run(c["pk"], c["min_interval"], c["counter"], c["epoch"],
             c["batch"], thief, c["sig"])


@pytest.mark.parametrize("field", ["counter", "epoch", "batch"])
def test_rejects_tampered_state(field):
    # the signature binds the whole transition; bumping any signed field fails.
    c = _signed_case()
    counter, epoch, batch = c["counter"], c["epoch"], c["batch"]
    if field == "counter":
        counter += 1
    elif field == "epoch":
        epoch += 1
    else:
        batch = hashlib.sha256(b"different-batch").digest()
    with pytest.raises(ValueError):
        _run(c["pk"], c["min_interval"], counter, epoch, batch, c["next_ph"], c["sig"])
