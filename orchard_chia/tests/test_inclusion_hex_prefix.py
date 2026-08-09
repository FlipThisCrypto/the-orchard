# SPDX-License-Identifier: Apache-2.0
"""DataLayer hex travels in two forms, and the verifier must not care which.

``batch_update`` keys are bare hex; everything a Chia node RETURNS is
0x-prefixed. Comparing the two forms directly produced two failures on the first
real publish (2026-08-08, node D8641AD6…, season 74 hour 14) — against data that
was byte-for-byte correct on chain and verified by hand with
``verify_proof -> current_root: true``:

  1. ``key_clvm_hash("0x…")`` raised ValueError, which ``_count_proven_keys``
     swallows, so a fully proven key was counted as unproven and the verdict was
     CANNOT-VERIFY ("proof covers 0/2 keys").
  2. The value-binding lookup was keyed by the node's prefixed hashes while our
     computed hash was bare, so ``.get()`` returned None and ``None != want`` was
     reported as "on-chain value differs (tampered or stale mirror)" — a verdict
     of INVALID.

(2) is the one that matters most. A verifier that cries tampering at honest data
destroys the trust it exists to establish, and "INVALID" is precisely the verdict
an operator is told to stop and investigate.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer import inclusion
from orchard_chia.datalayer.inclusion import clvm_hash, key_clvm_hash, _strip0x

# The real key/value hashes from that publish, as the node reported them.
KEY = "readings:D8641AD6CAE36977818499469F7E8C49:00000074:14".encode().hex()
NODE_KEY_HASH = "0xb1cf8bed6244ba3efb4637d16ed927601649a414bdf0a2421498ee0b0940bcb3"


def test_both_hex_forms_hash_identically():
    assert key_clvm_hash(KEY) == key_clvm_hash("0x" + KEY)
    assert key_clvm_hash("0X" + KEY) == key_clvm_hash(KEY)


def test_our_hash_matches_what_a_real_node_returned():
    # Not a self-consistency check — this is the value a live chia node produced
    # for this exact key.
    assert _strip0x(NODE_KEY_HASH) == key_clvm_hash(KEY)


def test_a_prefixed_key_is_counted_as_proven():
    # The CANNOT-VERIFY bug: prefixed key -> ValueError -> silently unproven.
    proof = {"success": True, "proof": {"store_proofs": {"proofs": [
        {"key_clvm_hash": NODE_KEY_HASH, "layers": []}]}}}
    assert inclusion._count_proven_keys(proof, [KEY]) == 1
    assert inclusion._count_proven_keys(proof, ["0x" + KEY]) == 1, (
        "a 0x-prefixed key must count as proven — it is the same key"
    )


def test_a_genuinely_absent_key_is_still_unproven():
    # The fix must not turn the check into a rubber stamp.
    other = "readings:FFFFFFFF:00000074:15".encode().hex()
    proof = {"success": True, "proof": {"store_proofs": {"proofs": [
        {"key_clvm_hash": NODE_KEY_HASH, "layers": []}]}}}
    assert inclusion._count_proven_keys(proof, [other]) == 0
    assert inclusion._count_proven_keys(proof, ["0x" + other]) == 0


def test_non_hex_is_still_rejected():
    with pytest.raises(ValueError):
        key_clvm_hash("not-hex-at-all")
    with pytest.raises(ValueError):
        key_clvm_hash("0xzzzz")


def test_value_binding_matches_across_prefix_forms():
    """The INVALID bug: bare vs prefixed made a correct value look tampered."""
    value_hex = "7b7d"                       # b"{}"
    vh = clvm_hash(value_hex)

    for stored_key, stored_val in (
        ("0x" + key_clvm_hash(KEY), "0x" + vh),   # exactly what a node returns
        (key_clvm_hash(KEY), vh),                 # bare, for symmetry
    ):
        table = {_strip0x(k): _strip0x(v) for k, v in {stored_key: stored_val}.items()}
        assert table.get(_strip0x(key_clvm_hash(KEY))) == _strip0x(clvm_hash(value_hex)), (
            "a correct on-chain value must bind regardless of hex form"
        )


def test_a_truly_different_value_still_fails_to_bind():
    # Tampering must still be caught — the whole point of value binding.
    table = {_strip0x(key_clvm_hash(KEY)): _strip0x(clvm_hash("7b7d"))}
    assert table.get(_strip0x(key_clvm_hash(KEY))) != _strip0x(clvm_hash("deadbeef"))


def test_strip0x_is_conservative():
    assert _strip0x("0xabc") == "abc"
    assert _strip0x("abc") == "abc"
    assert _strip0x("  0xABC  ") == "ABC"
    # Must not eat a leading "0" that is part of the hex.
    assert _strip0x("0abc") == "0abc"
