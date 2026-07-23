# SPDX-License-Identifier: Apache-2.0
"""Tests for DataLayer inclusion / get_proof helpers.

get_proof responses use the documented nested shape
``proof.store_proofs.proofs[]`` where each entry carries ``key_clvm_hash``
(not the plaintext key) — CHIA_DATALAYER_RPC.md §4. Keys are matched back by
their CLVM hash ``sha256(0x01 || key_bytes)``.
"""
from __future__ import annotations

from orchard_chia.datalayer import inclusion
from orchard_chia.datalayer.rpc import ChiaRpcError

# Golden CLVM hashes (independently pins the 0x01-prefix rule):
#   sha256(0x01 || bytes.fromhex(k))
CLVM = {
    "aa": "b4bc136e1fb4ea0b3340d06b158277c4a8537a13202f518e5b8c423273b9d7a0",
    "bb": "a149ab0dea34c7dbe2adf67232fff6ff605398ce4f59722f5e73e1ad7a803a2d",
    "cc": "8a817bc42b2e2146dc4ca4dc686db0a4051d294463d774fdac35bd191ae4a3b5",
}


def _proof_for(keys):
    """Build a realistic get_proof response covering the given key hexes."""
    return {
        "success": True,
        "proof": {
            "coin_id": "de" * 32,
            "inner_puzzle_hash": "ad" * 32,
            "store_proofs": {
                "store_id": "11" * 32,
                "proofs": [
                    {
                        "key_clvm_hash": CLVM[k],
                        "value_clvm_hash": "ff" * 32,
                        "node_hash": "ee" * 32,
                        "layers": [],
                    }
                    for k in keys
                ],
            },
        },
    }


class FakeRpc:
    def __init__(
        self,
        *,
        root=None,
        proof=None,
        root_err=None,
        proof_err=None,
        current_root=True,
        verify_err=None,
    ):
        self._root = root
        self._proof = proof
        self._root_err = root_err
        self._proof_err = proof_err
        self._current_root = current_root
        self._verify_err = verify_err

    def get_root(self, store_id: str) -> dict:
        if self._root_err:
            raise ChiaRpcError(self._root_err)
        return self._root

    def get_proof(self, store_id: str, keys_hex: list[str]) -> dict:
        if self._proof_err:
            raise ChiaRpcError(self._proof_err)
        return self._proof

    def verify_proof(self, coin_id, inner_puzzle_hash, store_proofs) -> dict:
        if self._verify_err:
            raise ChiaRpcError(self._verify_err)
        return {
            "success": True,
            "current_root": self._current_root,
            "verified_clvm_hashes": {"store_id": "11" * 32, "inclusions": []},
        }


def test_key_clvm_hash_golden():
    assert inclusion.key_clvm_hash("aa") == CLVM["aa"]
    assert inclusion.key_clvm_hash("bb") == CLVM["bb"]


def test_inclusion_ok_real_shape():
    keys = ["aa", "bb"]
    rpc = FakeRpc(
        root={"success": True, "hash": "ab" * 32, "confirmed": True},
        proof=_proof_for(keys),
        current_root=True,
    )
    rep = inclusion.check_inclusion(rpc, "store", keys)
    assert rep.ok is True
    assert rep.keys_proven == 2
    assert rep.confirmed is True
    assert rep.current_root is True
    assert rep.root_hash == "ab" * 32


def test_inclusion_fails_when_root_moved():
    # Keys prove out, but verify_proof says the current root has moved on.
    keys = ["aa"]
    rpc = FakeRpc(
        root={"success": True, "hash": "ab" * 32, "confirmed": True},
        proof=_proof_for(keys),
        current_root=False,
    )
    rep = inclusion.check_inclusion(rpc, "store", keys)
    assert rep.ok is False
    assert rep.current_root is False
    assert rep.keys_proven == 1
    assert "current_root=false" in rep.detail


def test_inclusion_fails_when_verify_proof_unreachable():
    keys = ["aa"]
    rpc = FakeRpc(
        root={"success": True, "hash": "ab" * 32, "confirmed": True},
        proof=_proof_for(keys),
        verify_err="verify_proof endpoint down",
    )
    rep = inclusion.check_inclusion(rpc, "store", keys)
    assert rep.ok is False
    assert "verify_proof failed" in rep.detail


def test_proof_envelope_extraction():
    env = inclusion.proof_envelope(_proof_for(["aa"]))
    assert env is not None
    assert env["coin_id"] == "de" * 32
    assert env["inner_puzzle_hash"] == "ad" * 32
    assert env["store_proofs"]["store_id"] == "11" * 32
    # Missing coin_id -> unusable
    assert inclusion.proof_envelope({"proof": {"store_proofs": {}}}) is None


def test_inclusion_fails_on_missing_proof_keys():
    rpc = FakeRpc(
        root={"success": True, "hash": "cd" * 32, "confirmed": True},
        proof=_proof_for(["aa"]),  # only aa proven; bb requested
    )
    rep = inclusion.check_inclusion(rpc, "store", ["aa", "bb"])
    assert rep.ok is False
    assert rep.keys_proven == 1


def test_inclusion_no_false_positive_on_empty_proofs():
    # A success envelope with an empty proofs list must NOT count as proven.
    rpc = FakeRpc(
        root={"success": True, "hash": "cd" * 32, "confirmed": True},
        proof={"success": True, "proof": {"store_proofs": {"proofs": []}}},
    )
    rep = inclusion.check_inclusion(rpc, "store", ["aa"])
    assert rep.ok is False
    assert rep.keys_proven == 0


def test_inclusion_mismatched_hash_not_proven():
    # Proof carries a clvm hash for cc, but we asked about aa/bb.
    rpc = FakeRpc(
        root={"success": True, "hash": "cd" * 32, "confirmed": True},
        proof=_proof_for(["cc"]),
    )
    rep = inclusion.check_inclusion(rpc, "store", ["aa", "bb"])
    assert rep.ok is False
    assert rep.keys_proven == 0


def test_inclusion_get_root_error():
    rpc = FakeRpc(root_err="down")
    rep = inclusion.check_inclusion(rpc, "store", ["aa"])
    assert rep.ok is False
    assert "get_root" in rep.detail


def test_inclusion_no_keys():
    rep = inclusion.check_inclusion(FakeRpc(root={}), "store", [])
    assert rep.ok is False
