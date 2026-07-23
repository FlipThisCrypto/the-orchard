# SPDX-License-Identifier: Apache-2.0
"""Request-body shape guards for DataLayerRpc.

Chia's DataLayer RPC is inconsistent about the store-ID parameter name:
``get_proof`` takes ``store_id`` while every other endpoint takes ``id``
(docs/datalayer/reference/CHIA_DATALAYER_RPC.md §4). Getting this wrong makes
a real node ignore the store, so pin the posted body for the endpoints we call.
"""
from __future__ import annotations

from orchard_chia.datalayer.rpc import DataLayerRpc
from orchard_chia.datalayer.retry import RetryPolicy


def _capturing_rpc(captured: dict) -> DataLayerRpc:
    dl = DataLayerRpc(
        "127.0.0.1", 1, "c", "k", retry_policy=RetryPolicy(max_attempts=1)
    )

    def fake_post(route, body):
        captured["route"] = route
        captured["body"] = body
        # Minimal well-formed responses per endpoint.
        if route == "get_proof":
            return {"success": True, "proof": {}}
        if route == "get_root":
            return {"success": True, "hash": "ab" * 32, "confirmed": True}
        return {"success": True}

    dl._post = fake_post  # type: ignore[assignment]
    return dl


def test_get_proof_uses_store_id_not_id():
    cap: dict = {}
    rpc = _capturing_rpc(cap)
    rpc.get_proof("STORE", ["aa", "bb"])
    assert cap["route"] == "get_proof"
    # The whole point: get_proof is the store_id outlier.
    assert cap["body"].get("store_id") == "STORE"
    assert "id" not in cap["body"], "get_proof must not send the 'id' param"
    assert cap["body"]["keys"] == ["aa", "bb"]


def test_get_root_uses_id():
    cap: dict = {}
    rpc = _capturing_rpc(cap)
    rpc.get_root("STORE")
    assert cap["body"] == {"id": "STORE"}


def test_batch_update_uses_id():
    cap: dict = {}
    rpc = _capturing_rpc(cap)
    changelist = [{"action": "insert", "key": "aa", "value": "bb"}]
    rpc.batch_update("STORE", changelist)
    assert cap["body"] == {"id": "STORE", "changelist": changelist}
