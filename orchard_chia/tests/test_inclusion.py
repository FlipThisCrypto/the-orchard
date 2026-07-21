# SPDX-License-Identifier: Apache-2.0
"""Tests for DataLayer inclusion / get_proof helpers."""
from __future__ import annotations

from orchard_chia.datalayer import inclusion
from orchard_chia.datalayer.rpc import ChiaRpcError


class FakeRpc:
    def __init__(self, *, root=None, proof=None, root_err=None, proof_err=None):
        self._root = root
        self._proof = proof
        self._root_err = root_err
        self._proof_err = proof_err

    def get_root(self, store_id: str) -> dict:
        if self._root_err:
            raise ChiaRpcError(self._root_err)
        return self._root

    def get_proof(self, store_id: str, keys_hex: list[str]) -> dict:
        if self._proof_err:
            raise ChiaRpcError(self._proof_err)
        return self._proof


def test_inclusion_ok_with_proofs_dict():
    keys = ["aa", "bb"]
    rpc = FakeRpc(
        root={"success": True, "hash": "ab" * 32, "confirmed": True},
        proof={"success": True, "proofs": {"aa": {"x": 1}, "bb": {"x": 2}}},
    )
    rep = inclusion.check_inclusion(rpc, "store", keys)
    assert rep.ok is True
    assert rep.keys_proven == 2
    assert rep.confirmed is True
    assert rep.root_hash == "ab" * 32


def test_inclusion_fails_on_missing_proof_keys():
    rpc = FakeRpc(
        root={"success": True, "hash": "cd" * 32, "confirmed": True},
        proof={"success": True, "proofs": {"aa": {}}},
    )
    rep = inclusion.check_inclusion(rpc, "store", ["aa", "bb"])
    assert rep.ok is False
    assert rep.keys_proven == 1


def test_inclusion_get_root_error():
    rpc = FakeRpc(root_err="down")
    rep = inclusion.check_inclusion(rpc, "store", ["aa"])
    assert rep.ok is False
    assert "get_root" in rep.detail


def test_inclusion_no_keys():
    rep = inclusion.check_inclusion(FakeRpc(root={}), "store", [])
    assert rep.ok is False
