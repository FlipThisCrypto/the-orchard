# SPDX-License-Identifier: Apache-2.0
"""An unreadable store is an error, never an empty payout ledger.

reader.read_all_attestations used the soft get_keys, which returns [] when the
store cannot be read. Zero keys -> zero entitlement rows -> "payable: 0" and
exit 0: a run that read NOTHING reporting itself as a correct run that owed
nobody. Before the pagination fix, that was every single run, live.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc
from orchard_chia.payout import reader


def _rpc(post):
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = post
    return r


def test_an_unreachable_store_raises_instead_of_paying_nobody():
    def dead(route, body, timeout=60):
        raise ChiaRpcError("connection refused")
    with pytest.raises(ChiaRpcError):
        reader.read_all_attestations(_rpc(dead), "store")


def test_a_readable_store_still_reads():
    from orchard_chia.datalayer import schema
    key_ascii = "attest:AABBCCDDEEFF0011AABBCCDDEEFF0011:00000005"
    key_hex = key_ascii.encode().hex()
    value_hex = schema.value_hex({"node_id": "AABBCCDDEEFF0011AABBCCDDEEFF0011",
                                  "season": 5, "hours_online": 3})

    def post(route, body, timeout=60):
        if route == "get_keys":
            return {"keys": [key_hex], "total_pages": 1}
        if route == "get_value":
            return {"value": value_hex}
        raise AssertionError(route)

    got = reader.read_all_attestations(_rpc(post), "store")
    assert len(got) == 1 and got[0].season == 5


def test_the_reader_uses_the_strict_variant():
    import inspect
    src = inspect.getsource(reader)
    assert "get_keys_strict" in src
