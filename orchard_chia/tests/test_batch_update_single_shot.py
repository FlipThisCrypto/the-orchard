# SPDX-License-Identifier: Apache-2.0
"""batch_update is submitted exactly once, whatever happens.

Every DataLayer route goes through the retry wrapper, and for reads that is
right. But a batch_update that times out may already be IN the mempool — the
wrapper cannot tell "never sent" from "sent, answer lost" — and retrying the
ambiguous case submits the same fee-bearing spend again: up to four root
updates for one approved plan, final state decided by mempool ordering. The
adversarial review flagged it; the caller's confirm step is the real retry.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc
from orchard_chia.datalayer.retry import RetryPolicy


def _dl(post_once):
    dl = DataLayerRpc.__new__(DataLayerRpc)
    dl._retry_policy = RetryPolicy(max_attempts=4, base_delay_s=0.0, max_delay_s=0.0)
    dl.last_retry_attempts = 1
    dl.last_retried = False
    dl._post_once = post_once
    return dl


def test_a_timeout_is_not_retried():
    calls = []

    def post_once(route, body):
        calls.append(route)
        raise ChiaRpcError("datalayer batch_update unreachable: timeout")

    with pytest.raises(ChiaRpcError):
        _dl(post_once).batch_update("STORE", [{"action": "insert",
                                               "key": "aa", "value": "bb"}])
    assert calls == ["batch_update"], (
        f"{len(calls)} submits for one plan — an ambiguous failure may already "
        f"be in the mempool, and each retry pays the fee again")


def test_reads_still_retry():
    """The single-shot rule is for the spend, not for the whole client."""
    attempts = []

    def post_once(route, body):
        attempts.append(route)
        if len(attempts) < 3:
            raise ChiaRpcError("datalayer get_root -> 503: busy")
        return {"success": True, "hash": "ab" * 32, "confirmed": True}

    dl = _dl(post_once)
    got = dl.get_root("STORE")
    assert got.get("hash") and len(attempts) == 3
