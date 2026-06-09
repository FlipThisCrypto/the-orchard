# SPDX-License-Identifier: Apache-2.0
"""Tests for orchard_chia.datalayer.merkle.

Hermetic. Validates the domain-separated SHA-256 tree against the committed
golden vectors (the cross-language contract) plus structural properties:
promote-last on odd levels, single-leaf identity, empty sentinel, and audit
proof round-trips for every leaf position.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchard_chia.datalayer import merkle

VEC = json.loads(
    (Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json")
    .read_text(encoding="utf-8")
)


def _leaves(case):
    return [bytes.fromhex(h) for h in case["leaves"]]


def test_empty_root_matches_vector():
    assert merkle.EMPTY_ROOT.hex() == VEC["merkle"]["empty_root"]
    assert merkle.merkle_root([]).hex() == VEC["merkle"]["empty_root"]


def test_domain_separation_prefixes():
    # A leaf and a node over the same bytes must differ (0x00 vs 0x01).
    x = hashlib.sha256(b"x").digest()
    assert merkle.leaf_hash(x) != merkle.node_hash(x, x)


@pytest.mark.parametrize("case", VEC["merkle"]["cases"])
def test_known_answer_roots(case):
    assert merkle.merkle_root(_leaves(case)).hex() == case["root"]


def test_single_leaf_is_its_own_root():
    leaf = _leaves(VEC["merkle"]["cases"][0])[0]
    assert merkle.merkle_root([leaf]) == leaf


def test_promote_last_is_not_duplicate():
    # 3 leaves: if the odd tail were DUPLICATED we'd get node_hash(L2,L2) at the
    # second slot; promotion carries L2 up unchanged. Assert the promotion path.
    leaves = _leaves(VEC["merkle"]["cases"][2])  # n == 3
    l0, l1, l2 = leaves
    expected = merkle.node_hash(merkle.node_hash(l0, l1), l2)
    duplicated = merkle.node_hash(merkle.node_hash(l0, l1), merkle.node_hash(l2, l2))
    assert merkle.merkle_root(leaves) == expected
    assert merkle.merkle_root(leaves) != duplicated


@pytest.mark.parametrize("case", VEC["merkle"]["cases"])
def test_proof_round_trips_for_every_leaf(case):
    leaves = _leaves(case)
    root = bytes.fromhex(case["root"])
    for i, leaf in enumerate(leaves):
        proof = merkle.merkle_proof(leaves, i)
        assert merkle.verify_proof(leaf, proof, root) is True


def test_proof_rejects_wrong_leaf():
    leaves = _leaves(VEC["merkle"]["cases"][4])  # n == 5
    root = bytes.fromhex(VEC["merkle"]["cases"][4]["root"])
    proof = merkle.merkle_proof(leaves, 0)
    wrong = hashlib.sha256(b"not-a-real-leaf").digest()
    assert merkle.verify_proof(wrong, proof, root) is False


def test_proof_rejects_tampered_path():
    leaves = _leaves(VEC["merkle"]["cases"][3])  # n == 4
    root = bytes.fromhex(VEC["merkle"]["cases"][3]["root"])
    proof = merkle.merkle_proof(leaves, 1)
    # Flip a bit in the first sibling.
    side, sib = proof[0]
    tampered = [(side, ("%064x" % (int(sib, 16) ^ 1)))] + proof[1:]
    assert merkle.verify_proof(leaves[1], tampered, root) is False


def test_proof_index_out_of_range():
    leaves = _leaves(VEC["merkle"]["cases"][1])
    with pytest.raises(IndexError):
        merkle.merkle_proof(leaves, 5)
