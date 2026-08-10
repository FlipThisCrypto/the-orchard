# SPDX-License-Identifier: Apache-2.0
"""The public hour count comes from the chain, not a local file.

latest:.running_hours_online was derived from the local watermark DB, so
pointing ORCHARD_PUBLISH_WATERMARK at a fresh path — a restore, a moved
checkout, a new machine — made the next publish rewrite the public record
DOWNWARD over hours genuinely on chain, permanently. The store itself is the
record; the watermark is only a skip-cache.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.publish import _count_chain_hours
from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc

NODE = "D8641AD6CAE36977818499469F7E8C49"


def _rpc(keys_ascii):
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = lambda route, body, timeout=60: {
        "keys": [k.encode().hex() for k in keys_ascii], "total_pages": 1}
    return r


def test_counts_readings_keys_per_node_season():
    keys = [f"readings:{NODE}:00000074:{h:02d}" for h in range(9)]
    keys += [f"attest:{NODE}:00000074", f"latest:{NODE}", "meta:schema"]
    got = _count_chain_hours(_rpc(keys), "store")
    assert got == {(NODE, 74): 9}


def test_seasons_are_counted_separately():
    keys = [f"readings:{NODE}:00000074:00", f"readings:{NODE}:00000075:00",
            f"readings:{NODE}:00000075:01"]
    got = _count_chain_hours(_rpc(keys), "store")
    assert got == {(NODE, 74): 1, (NODE, 75): 2}


def test_an_unreadable_store_raises_rather_than_counting_zero():
    """Zero here becomes the public running_hours_online."""
    def dead(route, body, timeout=60):
        raise ChiaRpcError("refused")
    r = DataLayerRpc.__new__(DataLayerRpc)
    r._post = dead
    with pytest.raises(ChiaRpcError):
        _count_chain_hours(r, "store")


def test_unicode_hour_tails_do_not_count():
    got = _count_chain_hours(_rpc([f"readings:{NODE}:00000074:٠٧"]), "store")
    assert got == {}


def test_malformed_keys_are_ignored():
    got = _count_chain_hours(_rpc(["readings:short", "notakey", ""]), "store")
    assert got == {}
